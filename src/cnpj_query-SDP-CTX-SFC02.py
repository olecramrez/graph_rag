import json
import os
import re
import sqlite3
import time
import unicodedata
from pathlib import Path

from src.config import get_cnpj_db_path
from src.lia_client import LIAClientError, chat_completion


CNPJ_TERMS = (
    "cnpj",
    "qsa",
    "razao social",
    "nome fantasia",
    "dados cadastrais",
    "cadastro",
    "socio",
    "socios",
    "administrador",
    "administradores",
    "representante legal",
    "quadro societario",
    "tem empresa",
    "todas as informacoes",
    "societaria",
    "societario",
    "relacao",
    "relacionamento",
    "vinculo",
    "vinculacao",
    "ligacao",
    "conexao",
    "cnae",
    "situacao cadastral",
    "estabelecimento",
    "matriz",
    "filial",
)

UF_SET = {
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT",
    "MS", "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO",
    "RR", "SC", "SP", "SE", "TO",
}

UF_NAME_MAP = {
    "acre": "AC",
    "alagoas": "AL",
    "amapa": "AP",
    "amazonas": "AM",
    "bahia": "BA",
    "ceara": "CE",
    "distrito federal": "DF",
    "espirito santo": "ES",
    "goias": "GO",
    "maranhao": "MA",
    "mato grosso": "MT",
    "mato grosso do sul": "MS",
    "minas gerais": "MG",
    "para": "PA",
    "paraiba": "PB",
    "parana": "PR",
    "pernambuco": "PE",
    "piaui": "PI",
    "rio de janeiro": "RJ",
    "rio grande do norte": "RN",
    "rio grande do sul": "RS",
    "rondonia": "RO",
    "roraima": "RR",
    "santa catarina": "SC",
    "sao paulo": "SP",
    "sergipe": "SE",
    "tocantins": "TO",
}

SITUACAO_LABELS = {
    "01": "Nula",
    "02": "Ativa",
    "03": "Suspensa",
    "04": "Inapta",
    "08": "Baixada",
}

PORTE_LABELS = {
    "00": "Nao informado",
    "01": "Microempresa",
    "03": "Empresa de pequeno porte",
    "05": "Demais",
}

ALLOWED_INTENTS = {
    "get_cnpj",
    "get_socios",
    "search_company",
    "list_establishments",
    "count_establishments",
    "count_by_uf",
}

ALLOWED_FILTERS = {
    "cnpj",
    "cnpj_basico",
    "name",
    "uf",
    "cnae",
    "cnae_prefix",
    "situacao_cadastral",
    "data_inicio_atividade_inicio",
    "data_inicio_atividade_fim",
}

RELATION_TERMS = (
    "relacao",
    "relacionamento",
    "relacionada",
    "relacionadas",
    "relacionado",
    "relacionados",
    "vinculo",
    "vinculacao",
    "vinculada",
    "vinculadas",
    "vinculado",
    "vinculados",
    "ligacao",
    "conexao",
    "ligada",
    "ligadas",
    "ligado",
    "ligados",
    "socio em comum",
    "socios em comum",
    "socio da empresa",
    "socios da empresa",
    "indireta",
    "indireto",
    "intermediaria",
    "intermediario",
    "grupo economico",
)

LEGAL_SUFFIX_TOKENS = {
    "ltda",
    "limitada",
    "sa",
    "s a",
    "s/a",
    "s.a",
    "s.a.",
    "s",
    "a",
    "eireli",
    "me",
    "epp",
    "mei",
    "ss",
    "s s",
    "scp",
    "spe",
    "holding",
    "participacoes",
    "participacao",
    "empreendimentos",
    "comercio",
    "comercial",
    "servicos",
    "servico",
    "industria",
    "industrial",
    "importacao",
    "exportacao",
}

COMPANY_STOPWORDS = {
    "empresa",
    "companhia",
    "cia",
    "grupo",
    "the",
    "and",
    "de",
    "da",
    "do",
    "das",
    "dos",
    "e",
    "em",
    "para",
}

ENABLE_SLOW_NAME_SCAN = (os.getenv("CNPJ_ENABLE_SLOW_NAME_SCAN") or "").strip().lower() in {
    "1",
    "true",
    "yes",
    "sim",
}
CNPJ_NAME_SEARCH_TIMEOUT_SECONDS = float(os.getenv("CNPJ_NAME_SEARCH_TIMEOUT_SECONDS") or "4")


def _strip_accents(text):
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _normalize(text):
    text = _strip_accents(text).lower()
    return re.sub(r"\s+", " ", text).strip()


def _company_tokens(text, keep_legal_suffix=False):
    normalized = _normalize(text)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    raw_tokens = [token for token in normalized.split() if len(token) >= 1]
    tokens = []
    for token in raw_tokens:
        if token in COMPANY_STOPWORDS:
            continue
        if not keep_legal_suffix and token in LEGAL_SUFFIX_TOKENS:
            continue
        tokens.append(token)
    return tokens


def _normalize_company_name(text):
    return " ".join(_company_tokens(text))


def _sqlite_company_norm(text):
    return _normalize_company_name(text)


def _ensure_sqlite_helpers(conn):
    try:
        conn.create_function("CNPJ_COMPANY_NORM", 1, _sqlite_company_norm)
    except sqlite3.Error:
        pass


def _compact_sql(sql):
    return re.sub(r"\s+", " ", str(sql or "")).strip()


def _log_query(progress_callback, label, sql=None, params=None):
    if not progress_callback:
        return
    message = f"[QUERY][CNPJ] {label}"
    if sql:
        message += f" | SQL: {_compact_sql(sql)}"
    if params is not None:
        message += f" | params={params}"
    try:
        progress_callback(message[:1800])
    except Exception:
        pass


def _prefix_upper_bound(prefix):
    prefix = str(prefix or "")
    if not prefix:
        return None
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def _company_prefixes_for_index(name):
    prefixes = []
    raw = re.sub(r"[^A-Za-z0-9 ]+", " ", _strip_accents(str(name or ""))).upper()
    raw = re.sub(r"\s+", " ", raw).strip()
    if raw:
        prefixes.append(raw)

    tokens = _company_tokens(name)
    if tokens and len(tokens[0]) >= 3:
        prefixes.append(tokens[0].upper())

    normalized = _normalize_company_name(name).upper()
    if normalized:
        prefixes.append(normalized)

    unique = []
    seen = set()
    for prefix in prefixes:
        if prefix and prefix not in seen:
            seen.add(prefix)
            unique.append(prefix)
    return unique


def _fetchall_with_budget(conn, sql, params, timeout_seconds, progress_callback=None, label=None):
    deadline = time.monotonic() + max(0.5, float(timeout_seconds or 0.5))

    def _stop_if_expired():
        return 1 if time.monotonic() > deadline else 0

    try:
        conn.set_progress_handler(_stop_if_expired, 1000)
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "interrupted" not in str(exc).lower():
            raise
        if progress_callback and label:
            progress_callback(f"[WARN] Consulta CNPJ por nome excedeu {timeout_seconds:.1f}s e foi interrompida: {label}")
        return []
    finally:
        conn.set_progress_handler(None, 0)


def _company_match_score(query_name, candidate_name):
    query_tokens = _company_tokens(query_name)
    candidate_tokens = set(_company_tokens(candidate_name))
    if not query_tokens or not candidate_tokens:
        return 0

    score = 0
    for token in query_tokens:
        if token in candidate_tokens:
            score += 4
        elif any(item.startswith(token) or token.startswith(item) for item in candidate_tokens if len(token) >= 3):
            score += 2

    query_norm = _normalize_company_name(query_name)
    candidate_norm = _normalize_company_name(candidate_name)
    if query_norm and candidate_norm:
        if query_norm == candidate_norm:
            score += 12
        elif query_norm in candidate_norm or candidate_norm in query_norm:
            score += 6

    return score


def _digits(text):
    return re.sub(r"\D", "", str(text or ""))


def _cnpj_digits(query):
    matches = re.findall(r"\d[\d./ -]{12,}\d", str(query or ""))
    for match in matches:
        digits = _digits(match)
        if len(digits) == 14:
            return digits

    digits = _digits(query)
    if len(digits) == 14:
        return digits
    return None


def _cnpj_basico_digits(query):
    digits = _digits(query)
    if len(digits) == 8:
        return digits
    return None


def _format_cnpj(cnpj_basico, cnpj_ordem, cnpj_dv):
    value = f"{cnpj_basico or ''}{cnpj_ordem or ''}{cnpj_dv or ''}"
    if len(value) != 14:
        return value
    return f"{value[:2]}.{value[2:5]}.{value[5:8]}/{value[8:12]}-{value[12:]}"


def _table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _connect(db_path=None):
    path = Path(db_path) if db_path else get_cnpj_db_path()
    if not path.exists():
        return None, path
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-200000")
        conn.execute("PRAGMA mmap_size=268435456")
        conn.execute("PRAGMA optimize")
    except sqlite3.Error:
        pass
    _ensure_sqlite_helpers(conn)
    return conn, path


def is_cnpj_query(query):
    qn = _normalize(query)
    if _cnpj_digits(query):
        return True
    if "empresa" in qn and any(term in qn for term in (
        "informacoes", "informacao", "dados", "cadastro", "cadastral",
        "socios", "socio", "administrador", "representante", "endereco",
        "situacao", "cnae", "porte", "capital", "tem empresa",
    )):
        return True
    return any(term in qn for term in CNPJ_TERMS)


def is_cnpj_relationship_query(query):
    qn = _normalize(query)
    return any(term in qn for term in RELATION_TERMS)


def _has_any_table(conn, table_names):
    return any(_table_exists(conn, name) for name in table_names)


def _query_by_cnpj(conn, cnpj_digits):
    cnpj_basico = cnpj_digits[:8]
    cnpj_ordem = cnpj_digits[8:12]
    cnpj_dv = cnpj_digits[12:]
    return conn.execute(
        """
        SELECT
            e.cnpj_basico,
            e.razao_social,
            e.natureza_juridica,
            e.capital_social,
            e.porte_empresa,
            est.cnpj_ordem,
            est.cnpj_dv,
            est.matriz_filial,
            est.nome_fantasia,
            est.situacao_cadastral,
            est.data_situacao_cadastral,
            est.data_inicio_atividade,
            est.cnae_fiscal_principal,
            est.tipo_logradouro,
            est.logradouro,
            est.numero,
            est.complemento,
            est.bairro,
            est.cep,
            est.uf,
            est.municipio,
            est.correio_eletronico
        FROM estabelecimentos est
        LEFT JOIN empresas e ON e.cnpj_basico = est.cnpj_basico
        WHERE est.cnpj_basico = ?
          AND est.cnpj_ordem = ?
          AND est.cnpj_dv = ?
        LIMIT 1
        """,
        (cnpj_basico, cnpj_ordem, cnpj_dv),
    ).fetchone()


def _query_by_basico(conn, cnpj_basico, limit):
    return conn.execute(
        """
        SELECT
            e.cnpj_basico,
            e.razao_social,
            est.cnpj_ordem,
            est.cnpj_dv,
            est.nome_fantasia,
            est.situacao_cadastral,
            est.cnae_fiscal_principal,
            est.uf,
            est.municipio
        FROM estabelecimentos est
        LEFT JOIN empresas e ON e.cnpj_basico = est.cnpj_basico
        WHERE est.cnpj_basico = ?
        ORDER BY est.cnpj_ordem
        LIMIT ?
        """,
        (cnpj_basico, limit),
    ).fetchall()


def _query_socios(conn, cnpj_basico, limit):
    if not _table_exists(conn, "socios"):
        return []
    return conn.execute(
        """
        SELECT
            nome_socio_razao_social,
            cpf_cnpj_socio,
            identificador_socio,
            qualificacao_socio,
            data_entrada_sociedade,
            pais,
            nome_representante,
            faixa_etaria
        FROM socios
        WHERE cnpj_basico = ?
        ORDER BY data_entrada_sociedade DESC
        LIMIT ?
        """,
        (cnpj_basico, limit),
    ).fetchall()


def _query_cnae_description(conn, code):
    if not code or not _table_exists(conn, "cnaes"):
        return ""
    row = conn.execute(
        "SELECT descricao FROM cnaes WHERE codigo = ? LIMIT 1",
        (str(code),),
    ).fetchone()
    return row["descricao"] if row and "descricao" in row.keys() else ""


def _format_cnae(conn, code):
    code = str(code or "").strip()
    if not code:
        return "Nao informado"
    description = _query_cnae_description(conn, code)
    return f"{code} - {description}" if description else code


def _query_company_headquarters(conn, cnpj_basico):
    return conn.execute(
        """
        SELECT
            e.cnpj_basico,
            e.razao_social,
            est.cnpj_ordem,
            est.cnpj_dv,
            est.nome_fantasia,
            est.situacao_cadastral,
            est.cnae_fiscal_principal,
            est.uf,
            est.municipio
        FROM empresas e
        LEFT JOIN estabelecimentos est
          ON est.cnpj_basico = e.cnpj_basico
         AND est.cnpj_ordem = '0001'
        WHERE e.cnpj_basico = ?
        LIMIT 1
        """,
        (cnpj_basico,),
    ).fetchone()


def _query_companies_for_socio_key(conn, socio_key, limit):
    if not _table_exists(conn, "socios"):
        return []

    key_type = socio_key.get("type")
    value = socio_key.get("value")
    if not value:
        return []

    if key_type == "cpf_cnpj":
        where_sql = "s.cpf_cnpj_socio = ?"
        params = (value, limit)
    else:
        where_sql = "s.nome_socio_razao_social = ? COLLATE NOCASE"
        params = (value, limit)

    return conn.execute(
        f"""
        SELECT
            s.cnpj_basico,
            s.nome_socio_razao_social,
            s.cpf_cnpj_socio,
            s.qualificacao_socio,
            s.data_entrada_sociedade,
            e.razao_social,
            est.cnpj_ordem,
            est.cnpj_dv,
            est.nome_fantasia,
            est.situacao_cadastral,
            est.cnae_fiscal_principal,
            est.uf,
            est.municipio
        FROM socios s
        LEFT JOIN empresas e ON e.cnpj_basico = s.cnpj_basico
        LEFT JOIN estabelecimentos est
          ON est.cnpj_basico = s.cnpj_basico
         AND est.cnpj_ordem = '0001'
        WHERE {where_sql}
        LIMIT ?
        """,
        params,
    ).fetchall()


def _query_companies_by_socio_name(conn, person_name, uf=None, limit=20):
    if not _table_exists(conn, "socios"):
        return []

    _ensure_sqlite_helpers(conn)
    tokens = _company_tokens(person_name)
    if not tokens:
        return []

    clauses = [
        "CNPJ_COMPANY_NORM(s.nome_socio_razao_social) LIKE ?"
        for _ in tokens[:5]
    ]
    params = [f"%{token}%" for token in tokens[:5]]
    if uf:
        clauses.append("est.uf = ?")
        params.append(uf)

    where_sql = " AND ".join(clauses)
    return conn.execute(
        f"""
        SELECT
            s.cnpj_basico,
            s.nome_socio_razao_social,
            s.cpf_cnpj_socio,
            s.identificador_socio,
            s.qualificacao_socio,
            s.data_entrada_sociedade,
            s.nome_representante,
            s.faixa_etaria,
            e.razao_social,
            est.cnpj_ordem,
            est.cnpj_dv,
            est.nome_fantasia,
            est.situacao_cadastral,
            est.cnae_fiscal_principal,
            est.uf,
            est.municipio
        FROM socios s
        LEFT JOIN empresas e ON e.cnpj_basico = s.cnpj_basico
        LEFT JOIN estabelecimentos est
          ON est.cnpj_basico = s.cnpj_basico
         AND est.cnpj_ordem = '0001'
        WHERE {where_sql}
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()


def _extract_uf(query):
    tokens = re.findall(r"\b[A-Z]{2}\b", str(query or "").upper())
    for token in tokens:
        if token in UF_SET:
            return token
    qn = _normalize(query)
    for name, uf in UF_NAME_MAP.items():
        if re.search(rf"\b{re.escape(name)}\b", qn):
            return uf
    return None


def _extract_cnae(query):
    match = re.search(r"\b\d{7}\b", str(query or ""))
    return match.group(0) if match else None


def _extract_name_query(query):
    text = re.sub(r"\d[\d./ -]{7,}\d", " ", str(query or ""))
    text = re.sub(
        r"\b(me|de|dê|da|do|das|dos|sobre|toda|todas|todo|todos|as|os|a|o|informacoes|informações|informacao|informação|disponiveis|disponíveis|disponivel|disponível|dados|cnpj|razao social|nome fantasia|empresa|buscar|procure|listar|liste|qual|quais|socio|socios|situacao|cadastral|por|uf|cnae|ativa|ativas|ativo|ativos)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\binform\w*\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" :,-")
    return text


def _extract_person_name_query(query):
    text = re.sub(r"\d[\d./ -]{7,}\d", " ", str(query or ""))
    text = re.sub(
        r"\b(procure|buscar|busque|verifique|consulta|consultar|cnpj|qsa|se|e|é|socio|sócio|socios|sócios|administrador|administradora|representante|legal|tem|alguma|algum|empresa|empresas|em|no|na|nos|nas|de|do|da|dos|das|uf|estado|sao paulo|são paulo|sp)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s+", " ", text).strip(" :,-?.")
    return text


def _fts_query(text):
    tokens = [token for token in _company_tokens(text) if len(token) >= 3]
    if not tokens:
        return ""
    return " AND ".join(f'"{token}"' for token in tokens[:6])


def _extract_json_object(text):
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?", "", raw, flags=re.IGNORECASE).strip()
        raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except Exception:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        return json.loads(raw[start:end + 1])
    raise ValueError("Resposta do LLM nao contem JSON valido.")


def _normalize_date_yyyymmdd(value):
    digits = _digits(value)
    if len(digits) == 8:
        return digits
    return None


def _sanitize_intent(intent):
    if not isinstance(intent, dict):
        return None

    name = str(intent.get("intent") or "").strip()
    if name not in ALLOWED_INTENTS:
        return None

    raw_filters = intent.get("filters")
    if not isinstance(raw_filters, dict):
        raw_filters = {}

    filters = {}
    for key, value in raw_filters.items():
        if key not in ALLOWED_FILTERS or value in (None, ""):
            continue
        if key in {"cnpj", "cnpj_basico", "cnae", "cnae_prefix"}:
            digits = _digits(value)
            if key == "cnpj" and len(digits) == 14:
                filters[key] = digits
            elif key == "cnpj_basico" and len(digits) == 8:
                filters[key] = digits
            elif key == "cnae" and len(digits) == 7:
                filters[key] = digits
            elif key == "cnae_prefix" and 1 <= len(digits) <= 7:
                filters[key] = digits
        elif key == "uf":
            uf = str(value or "").strip().upper()
            if uf in UF_SET:
                filters[key] = uf
        elif key == "situacao_cadastral":
            normalized = _normalize(value)
            if normalized in {"ativa", "ativo", "ativos", "ativas"}:
                filters[key] = "02"
            elif re.fullmatch(r"\d{2}", str(value or "").strip()):
                filters[key] = str(value).strip()
        elif key.startswith("data_inicio_atividade_"):
            date_value = _normalize_date_yyyymmdd(value)
            if date_value:
                filters[key] = date_value
        elif key == "name":
            name_value = re.sub(r"\s+", " ", str(value or "")).strip()
            if name_value:
                filters[key] = name_value[:120]

    try:
        limit = int(intent.get("limit") or 20)
    except Exception:
        limit = 20
    limit = max(1, min(limit, 100))

    return {
        "intent": name,
        "filters": filters,
        "limit": limit,
        "explanation": str(intent.get("explanation") or "").strip()[:240],
    }


def _llm_cnpj_intent(query, llm_model=None, progress_callback=None):
    prompt = f"""
Converta a pergunta em JSON para consulta segura de uma base SQLite de CNPJ.
Nao escreva SQL. Responda apenas JSON puro.

Intents permitidas:
- get_cnpj: dados cadastrais de um CNPJ completo
- get_socios: socios de um CNPJ completo ou cnpj_basico
- search_company: busca por razao social ou nome fantasia
- list_establishments: lista estabelecimentos com filtros
- count_establishments: conta estabelecimentos com filtros
- count_by_uf: conta por UF com filtros

Filtros permitidos:
- cnpj: 14 digitos
- cnpj_basico: 8 digitos
- name: texto de razao social/nome fantasia
- uf: sigla UF
- cnae: 7 digitos
- cnae_prefix: prefixo numerico do CNAE, 1 a 7 digitos
- situacao_cadastral: use "02" para ativa
- data_inicio_atividade_inicio: AAAAMMDD
- data_inicio_atividade_fim: AAAAMMDD

Formato:
{{
  "intent": "list_establishments",
  "filters": {{"uf": "DF", "situacao_cadastral": "02"}},
  "limit": 20,
  "explanation": "breve"
}}

Pergunta: {query}
""".strip()

    try:
        raw = chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "Voce extrai intencoes para consultas CNPJ. "
                        "Responda somente JSON valido, sem markdown."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_retries=2,
            llm_model=llm_model,
        )
        intent = _sanitize_intent(_extract_json_object(raw))
        if intent and progress_callback:
            progress_callback(
                f"[INFO] Intencao CNPJ LLM: {intent['intent']} {intent['filters']}"
            )
        return intent
    except (LIAClientError, ValueError, json.JSONDecodeError, TypeError) as exc:
        if progress_callback:
            progress_callback(f"[WARN] Falha ao interpretar CNPJ com LLM: {exc}")
        return None


def _llm_consolidate_answer(query, deterministic_answer, llm_model=None, progress_callback=None):
    source = str(deterministic_answer or "").strip()
    if not source:
        return source

    prompt = f"""
Pergunta do usuario:
{query}

Resultado estruturado obtido do SQLite CNPJ:
{source[:12000]}

Redija uma resposta em portugues, clara e objetiva, usando exclusivamente os dados acima.
Nao invente campos, nomes, contagens, datas ou inferencias.
Se houver tabela, preserve a tabela ou reproduza os mesmos registros em tabela Markdown.
Se o resultado for uma contagem, destaque o total.
""".strip()

    try:
        answer = chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "Voce consolida resultados de consulta SQLite da base CNPJ. "
                        "Use somente os dados fornecidos e preserve valores literais."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_retries=2,
            llm_model=llm_model,
        )
        answer = str(answer or "").strip()
        if answer:
            if progress_callback:
                progress_callback("[INFO] Resposta CNPJ consolidada pelo LLM.")
            return answer
    except LIAClientError as exc:
        if progress_callback:
            progress_callback(f"[WARN] Falha ao consolidar resposta CNPJ com LLM: {exc}")
    return source


def _search_by_name(conn, query, limit, progress_callback=None):
    name_query = _extract_name_query(query)
    return _search_company_by_literal_name(
        conn,
        name_query,
        limit,
        progress_callback=progress_callback,
    )


def _search_company_by_literal_name(conn, name, limit, progress_callback=None):
    name_query = re.sub(r"\s+", " ", str(name or "")).strip()
    if not name_query:
        return [], ""

    rows = []
    fts_attempted = False
    budget = CNPJ_NAME_SEARCH_TIMEOUT_SECONDS
    for prefix in _company_prefixes_for_index(name_query):
        if len(rows) >= limit:
            break
        upper = _prefix_upper_bound(prefix)
        if not upper:
            continue
        if progress_callback:
            progress_callback(f"[INFO] Busca CNPJ por prefixo indexavel: {prefix}")
        rows.extend(
            _fetchall_with_budget(
                conn,
                """
                SELECT
                    e.cnpj_basico,
                    e.razao_social,
                    est.cnpj_ordem,
                    est.cnpj_dv,
                    est.nome_fantasia,
                    est.situacao_cadastral,
                    est.cnae_fiscal_principal,
                    est.uf,
                    est.municipio
                FROM empresas e
                LEFT JOIN estabelecimentos est
                  ON est.cnpj_basico = e.cnpj_basico
                 AND est.cnpj_ordem = '0001'
                WHERE e.razao_social >= ?
                  AND e.razao_social < ?
                ORDER BY e.razao_social
                LIMIT ?
                """,
                (prefix, upper, max(limit * 2, 10)),
                budget,
                progress_callback,
                f"prefixo razao_social {prefix}",
            )
        )

    if rows:
        scored = []
        seen = set()
        for row in rows:
            key = row["cnpj_basico"]
            if key not in seen:
                seen.add(key)
                score = max(
                    _company_match_score(name_query, row["razao_social"]),
                    _company_match_score(name_query, row["nome_fantasia"]),
                )
                if score > 0:
                    scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [row for _, row in scored[:limit]], name_query

    fts_queries = []
    for candidate in (name_query, _normalize_company_name(name_query)):
        fts = _fts_query(candidate)
        if fts and fts not in fts_queries:
            fts_queries.append(fts)

    for fts in fts_queries:
        if len(rows) >= limit or not _table_exists(conn, "empresas_fts"):
            break
        fts_attempted = True
        if progress_callback:
            progress_callback(f"[INFO] Busca CNPJ FTS em razao social: {fts}")
        rows.extend(
            _fetchall_with_budget(
                conn,
                """
                SELECT
                    e.cnpj_basico,
                    e.razao_social,
                    est.cnpj_ordem,
                    est.cnpj_dv,
                    est.nome_fantasia,
                    est.situacao_cadastral,
                    est.cnae_fiscal_principal,
                    est.uf,
                    est.municipio
                FROM empresas_fts f
                JOIN empresas e ON e.cnpj_basico = f.cnpj_basico
                LEFT JOIN estabelecimentos est
                  ON est.cnpj_basico = e.cnpj_basico
                 AND est.cnpj_ordem = '0001'
                WHERE empresas_fts MATCH ?
                LIMIT ?
                """,
                (fts, max(limit * 2, 10)),
                budget,
                progress_callback,
                f"FTS empresas_fts {fts}",
            )
        )

    for fts in fts_queries:
        if len(rows) >= limit or not _table_exists(conn, "estabelecimentos_fts"):
            break
        fts_attempted = True
        if progress_callback:
            progress_callback(f"[INFO] Busca CNPJ FTS em nome fantasia: {fts}")
        rows.extend(
            _fetchall_with_budget(
                conn,
                """
                SELECT
                    e.cnpj_basico,
                    e.razao_social,
                    est.cnpj_ordem,
                    est.cnpj_dv,
                    est.nome_fantasia,
                    est.situacao_cadastral,
                    est.cnae_fiscal_principal,
                    est.uf,
                    est.municipio
                FROM estabelecimentos_fts f
                JOIN estabelecimentos est
                  ON est.cnpj_basico = f.cnpj_basico
                 AND est.cnpj_ordem = f.cnpj_ordem
                 AND est.cnpj_dv = f.cnpj_dv
                LEFT JOIN empresas e ON e.cnpj_basico = est.cnpj_basico
                WHERE estabelecimentos_fts MATCH ?
                LIMIT ?
                """,
                (fts, max(limit * 2, 10)),
                budget,
                progress_callback,
                f"FTS estabelecimentos_fts {fts}",
            )
        )

    has_fts = _table_exists(conn, "empresas_fts")
    has_any_fts = has_fts or _table_exists(conn, "estabelecimentos_fts")
    if not rows and (not has_any_fts or not fts_attempted):
        like_names = [name_query, _normalize_company_name(name_query)]
        for like_name in like_names:
            if len(rows) >= limit or not like_name:
                break
            like = f"{like_name}%"
            rows.extend(
                _fetchall_with_budget(
                    conn,
                    """
                    SELECT
                        e.cnpj_basico,
                        e.razao_social,
                        est.cnpj_ordem,
                        est.cnpj_dv,
                        est.nome_fantasia,
                        est.situacao_cadastral,
                        est.cnae_fiscal_principal,
                        est.uf,
                        est.municipio
                    FROM empresas e
                    LEFT JOIN estabelecimentos est
                      ON est.cnpj_basico = e.cnpj_basico
                     AND est.cnpj_ordem = '0001'
                    WHERE e.razao_social LIKE ?
                    LIMIT ?
                    """,
                    (like, max(limit * 2, 10)),
                    budget,
                    progress_callback,
                    f"LIKE razao_social {like}",
                )
            )

    if not rows and (not has_any_fts or not fts_attempted):
        tokens = _company_tokens(name_query)
        for token in tokens[:1]:
            if len(rows) >= limit:
                break
            if len(token) < 5:
                continue
            like = f"{token}%"
            rows.extend(
                _fetchall_with_budget(
                    conn,
                    """
                    SELECT
                        e.cnpj_basico,
                        e.razao_social,
                        est.cnpj_ordem,
                        est.cnpj_dv,
                        est.nome_fantasia,
                        est.situacao_cadastral,
                        est.cnae_fiscal_principal,
                        est.uf,
                        est.municipio
                    FROM empresas e
                    LEFT JOIN estabelecimentos est
                      ON est.cnpj_basico = e.cnpj_basico
                     AND est.cnpj_ordem = '0001'
                    WHERE e.razao_social LIKE ?
                    LIMIT 250
                    """,
                    (like,),
                    budget,
                    progress_callback,
                    f"LIKE primeiro token {like}",
                )
            )

    if not rows and not has_any_fts:
        # Sem FTS, evitamos contains-search em tabela nacional inteira.
        # Use tools/optimize_cnpj_sqlite.py para ativar busca textual rápida.
        return [], name_query

    if ENABLE_SLOW_NAME_SCAN and not rows:
        _ensure_sqlite_helpers(conn)
        tokens = _company_tokens(name_query)
        if tokens:
            clauses = []
            params = []
            for token in tokens[:5]:
                clauses.append(
                    "CNPJ_COMPANY_NORM(COALESCE(e.razao_social, '') || ' ' || COALESCE(est.nome_fantasia, '')) LIKE ?"
                )
                params.append(f"%{token}%")
            where_sql = " AND ".join(clauses)
            rows.extend(
                _fetchall_with_budget(
                    conn,
                    f"""
                    SELECT
                        e.cnpj_basico,
                        e.razao_social,
                        est.cnpj_ordem,
                        est.cnpj_dv,
                        est.nome_fantasia,
                        est.situacao_cadastral,
                        est.cnae_fiscal_principal,
                        est.uf,
                        est.municipio
                    FROM empresas e
                    LEFT JOIN estabelecimentos est
                      ON est.cnpj_basico = e.cnpj_basico
                     AND est.cnpj_ordem = '0001'
                    WHERE {where_sql}
                    LIMIT 250
                    """,
                    tuple(params),
                    budget,
                    progress_callback,
                    "varredura lenta normalizada",
                )
            )

    if ENABLE_SLOW_NAME_SCAN and not rows:
        _ensure_sqlite_helpers(conn)
        tokens = _company_tokens(name_query)
        broad_tokens = [token[:6] for token in tokens[:2] if len(token) >= 5]
        for token in broad_tokens:
            if len(rows) >= limit:
                break
            rows.extend(
                _fetchall_with_budget(
                    conn,
                    """
                    SELECT
                        e.cnpj_basico,
                        e.razao_social,
                        est.cnpj_ordem,
                        est.cnpj_dv,
                        est.nome_fantasia,
                        est.situacao_cadastral,
                        est.cnae_fiscal_principal,
                        est.uf,
                        est.municipio
                    FROM empresas e
                    LEFT JOIN estabelecimentos est
                      ON est.cnpj_basico = e.cnpj_basico
                     AND est.cnpj_ordem = '0001'
                    WHERE CNPJ_COMPANY_NORM(COALESCE(e.razao_social, '') || ' ' || COALESCE(est.nome_fantasia, '')) LIKE ?
                    LIMIT 500
                    """,
                    (f"%{token}%",),
                    budget,
                    progress_callback,
                    f"varredura ampla token {token}",
                )
            )

    scored = []
    seen = set()
    for row in rows:
        key = row["cnpj_basico"]
        if key not in seen:
            seen.add(key)
            score = max(
                _company_match_score(name_query, row["razao_social"]),
                _company_match_score(name_query, row["nome_fantasia"]),
            )
            if score > 0:
                scored.append((score, row))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in scored[:limit]], name_query


def _filter_estabelecimentos(conn, query, limit):
    uf = _extract_uf(query)
    cnae = _extract_cnae(query)
    qn = _normalize(query)
    only_active = any(term in qn for term in ("ativa", "ativas", "ativo", "ativos"))

    clauses = []
    params = []
    if uf:
        clauses.append("est.uf = ?")
        params.append(uf)
    if cnae:
        clauses.append("est.cnae_fiscal_principal = ?")
        params.append(cnae)
    if only_active:
        clauses.append("est.situacao_cadastral = '02'")

    if not clauses:
        return [], {}

    where_sql = " AND ".join(clauses)
    if "quant" in qn and "por uf" in qn:
        rows = conn.execute(
            f"""
            SELECT est.uf, COUNT(*) AS total
            FROM estabelecimentos est
            WHERE {where_sql}
            GROUP BY est.uf
            ORDER BY total DESC
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        return rows, {"aggregate": "uf", "uf": uf, "cnae": cnae, "only_active": only_active}

    rows = conn.execute(
        f"""
        SELECT
            e.cnpj_basico,
            e.razao_social,
            est.cnpj_ordem,
            est.cnpj_dv,
            est.nome_fantasia,
            est.situacao_cadastral,
            est.cnae_fiscal_principal,
            est.uf,
            est.municipio
        FROM estabelecimentos est
        LEFT JOIN empresas e ON e.cnpj_basico = est.cnpj_basico
        WHERE {where_sql}
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    return rows, {"uf": uf, "cnae": cnae, "only_active": only_active}


def _where_from_filters(filters, alias="est"):
    clauses = []
    params = []

    cnpj = filters.get("cnpj")
    if cnpj:
        clauses.extend([
            f"{alias}.cnpj_basico = ?",
            f"{alias}.cnpj_ordem = ?",
            f"{alias}.cnpj_dv = ?",
        ])
        params.extend([cnpj[:8], cnpj[8:12], cnpj[12:]])

    if filters.get("cnpj_basico"):
        clauses.append(f"{alias}.cnpj_basico = ?")
        params.append(filters["cnpj_basico"])

    if filters.get("uf"):
        clauses.append(f"{alias}.uf = ?")
        params.append(filters["uf"])

    if filters.get("cnae"):
        clauses.append(f"{alias}.cnae_fiscal_principal = ?")
        params.append(filters["cnae"])

    if filters.get("cnae_prefix"):
        clauses.append(f"{alias}.cnae_fiscal_principal LIKE ?")
        params.append(filters["cnae_prefix"] + "%")

    if filters.get("situacao_cadastral"):
        clauses.append(f"{alias}.situacao_cadastral = ?")
        params.append(filters["situacao_cadastral"])

    if filters.get("data_inicio_atividade_inicio"):
        clauses.append(f"{alias}.data_inicio_atividade >= ?")
        params.append(filters["data_inicio_atividade_inicio"])

    if filters.get("data_inicio_atividade_fim"):
        clauses.append(f"{alias}.data_inicio_atividade <= ?")
        params.append(filters["data_inicio_atividade_fim"])

    return clauses, params


def _list_establishments_by_filters(conn, filters, limit):
    clauses, params = _where_from_filters(filters)
    name = filters.get("name")
    if name:
        clauses.append("(e.razao_social LIKE ? OR est.nome_fantasia LIKE ?)")
        like = f"%{name}%"
        params.extend([like, like])

    if not clauses:
        return []

    where_sql = " AND ".join(clauses)
    return conn.execute(
        f"""
        SELECT
            e.cnpj_basico,
            e.razao_social,
            est.cnpj_ordem,
            est.cnpj_dv,
            est.nome_fantasia,
            est.situacao_cadastral,
            est.cnae_fiscal_principal,
            est.uf,
            est.municipio
        FROM estabelecimentos est
        LEFT JOIN empresas e ON e.cnpj_basico = est.cnpj_basico
        WHERE {where_sql}
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()


def _count_establishments_by_filters(conn, filters):
    clauses, params = _where_from_filters(filters)
    name = filters.get("name")
    if name:
        clauses.append("(e.razao_social LIKE ? OR est.nome_fantasia LIKE ?)")
        like = f"%{name}%"
        params.extend([like, like])
    where_sql = " AND ".join(clauses) if clauses else "1 = 1"
    return conn.execute(
        f"""
        SELECT COUNT(*) AS total
        FROM estabelecimentos est
        LEFT JOIN empresas e ON e.cnpj_basico = est.cnpj_basico
        WHERE {where_sql}
        """,
        tuple(params),
    ).fetchone()["total"]


def _count_by_uf(conn, filters, limit):
    clauses, params = _where_from_filters(filters)
    name = filters.get("name")
    if name:
        clauses.append("(e.razao_social LIKE ? OR est.nome_fantasia LIKE ?)")
        like = f"%{name}%"
        params.extend([like, like])
    where_sql = " AND ".join(clauses) if clauses else "1 = 1"
    return conn.execute(
        f"""
        SELECT est.uf, COUNT(*) AS total
        FROM estabelecimentos est
        LEFT JOIN empresas e ON e.cnpj_basico = est.cnpj_basico
        WHERE {where_sql}
        GROUP BY est.uf
        ORDER BY total DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()


def _sanitize_company_mentions(payload):
    if not isinstance(payload, dict):
        return [], 2

    raw_companies = payload.get("companies")
    if not isinstance(raw_companies, list):
        raw_companies = []

    companies = []
    seen = set()
    for item in raw_companies:
        if not isinstance(item, dict):
            continue
        cnpj = _digits(item.get("cnpj"))
        name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip(" ,.;:-")
        if cnpj and len(cnpj) != 14:
            cnpj = ""
        if not cnpj and len(name) < 3:
            continue
        key = cnpj or _normalize(name)
        if key in seen:
            continue
        seen.add(key)
        companies.append({"cnpj": cnpj, "name": name})

    try:
        max_depth = int(payload.get("max_depth") or 2)
    except Exception:
        max_depth = 2
    max_depth = max(1, min(max_depth, 3))
    return companies[:4], max_depth


def _extract_relationship_mentions_fallback(query):
    mentions = [{"cnpj": cnpj, "name": ""} for cnpj in re.findall(r"\d[\d./ -]{12,}\d", str(query or "")) if len(_digits(cnpj)) == 14]

    text = re.sub(r"\d[\d./ -]{12,}\d", " ", str(query or ""))
    relation_pair_patterns = [
        r"\bentre\s+(.+?)\s+e\s+(.+?)(?:[?.!]|$)",
        r"\brelacao\s+(?:entre|de)\s+(.+?)\s+(?:e|com)\s+(.+?)(?:[?.!]|$)",
        r"\bvinculo\s+(?:entre|de)\s+(.+?)\s+(?:e|com)\s+(.+?)(?:[?.!]|$)",
        r"\bligacao\s+(?:entre|de)\s+(.+?)\s+(?:e|com)\s+(.+?)(?:[?.!]|$)",
    ]
    normalized_text = _strip_accents(text)
    for pattern in relation_pair_patterns:
        match = re.search(pattern, normalized_text, flags=re.IGNORECASE)
        if match:
            for group in match.groups():
                name = re.sub(
                    r"\b(alguma|societaria|societario|indireta|indireto|direta|direto)\b",
                    " ",
                    group,
                    flags=re.IGNORECASE,
                )
                name = re.sub(r"\s+", " ", name).strip(" ,.;:-")
                if len(name) >= 4:
                    mentions.append({"cnpj": "", "name": name})
            if len(mentions) >= 2:
                break

    text = re.sub(
        r"\b(empresa|empresas|cnpj|relacao|relação|relacionamento|vinculo|vínculo|ligacao|ligação|indireta|indireto|socio|sócio|socios|sócios|tem|alguma|entre|com|da|de|do|que|e|é|existe|verifique|procure|buscar|busque)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"\b(vinculacao|vincula..o|conexao|conex.o|outra|outras)\b",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    candidates = [
        part.strip(" ,.;:-")
        for part in re.split(r"\s{2,}|[;/]", text)
        if len(part.strip(" ,.;:-")) >= 4
    ]
    for candidate in candidates[:4]:
        mentions.append({"cnpj": "", "name": candidate})

    unique = []
    seen = set()
    for mention in mentions:
        key = mention["cnpj"] or _normalize(mention["name"])
        if key and key not in seen:
            seen.add(key)
            unique.append(mention)
    return unique[:4], 2


def _extract_relationship_mentions(query, llm_model=None, progress_callback=None):
    prompt = f"""
Extraia empresas citadas em uma pergunta sobre relacao societaria CNPJ.
Responda somente JSON puro, sem markdown.

Formato:
{{
  "companies": [
    {{"name": "EMPRESA X LTDA", "cnpj": "00000000000191"}},
    {{"name": "EMPRESA Y SA", "cnpj": ""}}
  ],
  "max_depth": 2
}}

Regras:
- Use cnpj somente se houver CNPJ completo de 14 digitos na pergunta.
- Inclua nomes empresariais literais citados pelo usuario.
- max_depth deve ser 1, 2 ou 3. Use 2 por padrao e 3 quando houver pedido de relacao indireta/intermediaria.
- Nao invente empresas.

Pergunta:
{query}
""".strip()

    try:
        raw = chat_completion(
            [
                {
                    "role": "system",
                    "content": (
                        "Voce extrai entidades empresariais para busca societaria. "
                        "Retorne apenas JSON valido."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_retries=2,
            llm_model=llm_model,
        )
        mentions, max_depth = _sanitize_company_mentions(_extract_json_object(raw))
        if len(mentions) >= 2:
            if progress_callback:
                progress_callback(f"[INFO] Empresas extraidas para grafo CNPJ: {mentions}")
            return mentions, max_depth
    except (LIAClientError, ValueError, json.JSONDecodeError, TypeError) as exc:
        if progress_callback:
            progress_callback(f"[WARN] Falha ao extrair empresas da relacao CNPJ com LLM: {exc}")

    return _extract_relationship_mentions_fallback(query)


def _company_label(row_or_item):
    if not row_or_item:
        return "Empresa nao identificada"
    cnpj = _format_cnpj(
        row_or_item["cnpj_basico"],
        row_or_item["cnpj_ordem"] or "",
        row_or_item["cnpj_dv"] or "",
    )
    name = row_or_item["razao_social"] or row_or_item["nome_fantasia"] or row_or_item["cnpj_basico"]
    return f"{name} ({cnpj})"


def _resolve_company_mentions(conn, mentions, per_mention_limit=3, progress_callback=None):
    resolved = []
    evidence = []

    for mention in mentions:
        rows = []
        cnpj = mention.get("cnpj") or ""
        name = mention.get("name") or ""

        if cnpj:
            row = _query_by_cnpj(conn, cnpj)
            if row is not None:
                rows = [row]
            evidence.append(f"Resolucao por CNPJ `{cnpj}`: {len(rows)} candidato(s).")
        elif name:
            rows, name_query = _search_company_by_literal_name(
                conn,
                name,
                per_mention_limit,
                progress_callback=progress_callback,
            )
            evidence.append(f"Resolucao por nome `{name_query or name}`: {len(rows)} candidato(s).")

        unique = []
        seen = set()
        for row in rows:
            key = row["cnpj_basico"]
            if key not in seen:
                seen.add(key)
                unique.append(row)

        if unique:
            resolved.append({"mention": mention, "candidates": unique[:per_mention_limit]})

    return resolved, evidence


def _socio_key(row):
    cpf_cnpj = str(row["cpf_cnpj_socio"] or "").strip()
    digits = _digits(cpf_cnpj)
    if digits and len(digits) >= 6 and len(set(digits)) > 1:
        return {"type": "cpf_cnpj", "value": cpf_cnpj, "strength": "forte"}

    name = re.sub(r"\s+", " ", str(row["nome_socio_razao_social"] or "")).strip()
    if name:
        return {"type": "nome", "value": name, "strength": "fraco"}
    return None


def _socio_label(row):
    name = row["nome_socio_razao_social"] or "Socio nao identificado"
    doc = row["cpf_cnpj_socio"] or "sem CPF/CNPJ"
    qual = row["qualificacao_socio"] or "qualificacao nao informada"
    return f"{name} ({doc}; {qual})"


def _path_company_depth(path):
    return sum(1 for node in path if node.get("type") == "company") - 1


def _format_relationship_path(path):
    parts = []
    for node in path:
        if node["type"] == "company":
            parts.append(_company_label(node["row"]))
        elif node["type"] == "socio":
            parts.append(_socio_label(node["row"]))
    return " -> ".join(parts)


def _find_relationship_paths(conn, source_basicos, target_basicos, max_depth=2, max_paths=5, per_socio_company_limit=100):
    target_set = set(target_basicos)
    found = []
    queue = []
    visited_companies = set(source_basicos)
    visited_socio_keys = set()

    for cnpj_basico in source_basicos:
        row = _query_company_headquarters(conn, cnpj_basico)
        if row is not None:
            queue.append((cnpj_basico, [{"type": "company", "row": row}]))

    while queue and len(found) < max_paths:
        cnpj_basico, path = queue.pop(0)
        if _path_company_depth(path) >= max_depth:
            continue

        socios = _query_socios(conn, cnpj_basico, 500)
        for socio in socios:
            key = _socio_key(socio)
            if not key:
                continue
            key_id = (key["type"], key["value"])
            if key_id in visited_socio_keys and key["type"] == "nome":
                continue
            visited_socio_keys.add(key_id)

            companies = _query_companies_for_socio_key(conn, key, per_socio_company_limit)
            for company in companies:
                next_basico = company["cnpj_basico"]
                if next_basico == cnpj_basico:
                    continue

                next_company_row = _query_company_headquarters(conn, next_basico) or company
                next_path = [
                    *path,
                    {"type": "socio", "row": socio, "key": key},
                    {"type": "company", "row": next_company_row},
                ]

                if next_basico in target_set:
                    found.append(next_path)
                    if len(found) >= max_paths:
                        break

                if next_basico not in visited_companies and _path_company_depth(next_path) < max_depth:
                    visited_companies.add(next_basico)
                    queue.append((next_basico, next_path))
            if len(found) >= max_paths:
                break

    return found


def answer_relationship_query(conn, query, llm_model=None, progress_callback=None):
    if not _table_exists(conn, "socios"):
        return "A tabela `socios` nao esta disponivel no SQLite CNPJ.", [
            "Busca societaria exige a tabela `socios`."
        ], False

    mentions, max_depth = _extract_relationship_mentions(
        query,
        llm_model=llm_model,
        progress_callback=progress_callback,
    )
    _log_query(
        progress_callback,
        "Resolver empresas citadas para grafo societario",
        "Para cada empresa: CNPJ exato ou busca por razao social/nome fantasia",
        {"mentions": mentions, "max_depth": max_depth},
    )
    resolved, evidence = _resolve_company_mentions(
        conn,
        mentions,
        progress_callback=progress_callback,
    )
    if len(resolved) < 2:
        return (
            "Nao consegui identificar duas empresas para comparar. "
            "Informe dois CNPJs completos ou duas razoes sociais mais literais."
        ), evidence, False

    source = resolved[0]
    targets = resolved[1:]
    source_basicos = [row["cnpj_basico"] for row in source["candidates"]]
    source_set = set(source_basicos)
    target_basicos = [
        row["cnpj_basico"]
        for item in targets
        for row in item["candidates"]
        if row["cnpj_basico"] not in source_set
    ]
    if not target_basicos:
        return (
            "Consegui resolver a empresa de origem, mas nao identifiquei uma segunda empresa distinta para comparar."
        ), evidence, False

    _log_query(
        progress_callback,
        "Expandir grafo societario",
        "BFS: empresas -> socios -> empresas usando socios.cpf_cnpj_socio ou nome_socio_razao_social",
        {"origens": source_basicos, "destinos": target_basicos, "max_depth": max_depth},
    )
    paths = _find_relationship_paths(
        conn,
        source_basicos,
        target_basicos,
        max_depth=max_depth,
    )

    source_labels = ", ".join(_company_label(row) for row in source["candidates"])
    target_labels = ", ".join(
        _company_label(row)
        for item in targets
        for row in item["candidates"]
    )

    if not paths:
        evidence.append(
            f"Busca em grafo societario ate {max_depth} salto(s) empresa-socio-empresa; nenhum caminho encontrado."
        )
        return (
            "Nao encontrei relacao societaria direta ou indireta entre os candidatos resolvidos "
            f"ate {max_depth} salto(s).\n\n"
            f"**Origem analisada:** {source_labels}\n\n"
            f"**Destino(s) analisado(s):** {target_labels}\n\n"
            "Isso nao prova ausencia absoluta de relacao; indica apenas que nao apareceu caminho na tabela `socios` carregada."
        ), evidence, True

    rows = []
    for idx, path in enumerate(paths, start=1):
        strengths = [
            node.get("key", {}).get("strength")
            for node in path
            if node.get("type") == "socio"
        ]
        confidence = "forte" if strengths and all(item == "forte" for item in strengths) else "indicio"
        rows.append([idx, _path_company_depth(path), confidence, _format_relationship_path(path)])

    evidence.append(
        f"Busca em grafo societario ate {max_depth} salto(s) empresa-socio-empresa."
    )
    evidence.append(
        "Confianca `forte` usa CPF/CNPJ de socio; `indicio` pode depender de nome de socio."
    )
    answer = (
        f"Encontrei {len(paths)} caminho(s) societario(s) entre os candidatos resolvidos.\n\n"
        + _md_table(["#", "Saltos", "Confianca", "Caminho"], rows)
    )
    return answer, evidence, True


def is_company_links_query(query):
    qn = _normalize(query)
    asks_other_companies = any(term in qn for term in (
        "outra empresa",
        "outras empresas",
        "demais empresas",
        "donos de outras",
        "sao donos",
        "sao socios",
        "tem socios que",
    ))
    asks_open_link = any(term in qn for term in (
        "alguma vinculacao",
        "algum vinculo",
        "alguma ligacao",
        "alguma conexao",
        "vinculacao com alguma",
        "vinculo com alguma",
    ))
    return "empresa" in qn and (asks_other_companies or asks_open_link)


def _resolve_single_company_for_links(conn, query, limit=3, llm_model=None, progress_callback=None):
    cnpj = _cnpj_digits(query)
    evidence = []
    if cnpj:
        row = _query_by_cnpj(conn, cnpj)
        evidence.append(f"Resolucao por CNPJ `{cnpj}`: {1 if row else 0} candidato(s).")
        return ([row] if row is not None else []), evidence

    mentions, _ = _extract_relationship_mentions(
        query,
        llm_model=llm_model,
        progress_callback=progress_callback,
    )
    if mentions:
        resolved, resolved_evidence = _resolve_company_mentions(
            conn,
            mentions[:1],
            per_mention_limit=limit,
            progress_callback=progress_callback,
        )
        evidence.extend(resolved_evidence)
        if resolved:
            return resolved[0]["candidates"], evidence

    name_query = _extract_name_query(query)
    rows, resolved_name = _search_company_by_literal_name(
        conn,
        name_query,
        limit,
        progress_callback=progress_callback,
    )
    evidence.append(f"Resolucao por nome `{resolved_name or name_query}`: {len(rows)} candidato(s).")
    return rows, evidence


def _query_linked_companies_for_company_socios(conn, cnpj_basico, limit=100):
    socios = _query_socios(conn, cnpj_basico, 500)
    linked = []
    seen = set()

    for socio in socios:
        key = _socio_key(socio)
        if not key:
            continue
        for company in _query_companies_for_socio_key(conn, key, limit):
            if company["cnpj_basico"] == cnpj_basico:
                continue
            row_key = (company["cnpj_basico"], key["type"], key["value"])
            if row_key in seen:
                continue
            seen.add(row_key)
            linked.append(company)
            if len(linked) >= limit:
                return socios, linked

    return socios, linked


def answer_company_links_query(conn, query, limit=50, llm_model=None, progress_callback=None):
    if not _table_exists(conn, "socios"):
        return "A tabela `socios` nao esta disponivel no SQLite CNPJ.", [
            "Busca de vinculos societarios exige a tabela `socios`."
        ], False

    candidates, evidence = _resolve_single_company_for_links(
        conn,
        query,
        limit=3,
        llm_model=llm_model,
        progress_callback=progress_callback,
    )
    if not candidates:
        return (
            "Nao consegui identificar a empresa para buscar vinculos societarios. "
            "Informe um CNPJ completo ou uma razao social mais literal."
        ), evidence, False

    sections = []
    total_links = 0
    for idx, row in enumerate(candidates[:3], start=1):
        source_label = _company_label(row)
        _log_query(
            progress_callback,
            "Buscar outras empresas vinculadas pelos socios/QSA",
            "empresa -> socios -> outras empresas usando socios.cpf_cnpj_socio ou nome_socio_razao_social",
            {"cnpj_basico": row["cnpj_basico"], "limit": limit},
        )
        socios, linked = _query_linked_companies_for_company_socios(
            conn,
            row["cnpj_basico"],
            limit=limit,
        )
        total_links += len(linked)
        if linked:
            body = (
                f"## Empresa {idx}: {source_label}\n\n"
                f"Encontrei {len(linked)} vinculo(s) com outras empresas por socio/administrador em comum.\n\n"
                + _format_person_company_rows(conn, linked)
            )
        else:
            body = (
                f"## Empresa {idx}: {source_label}\n\n"
                "Nao encontrei outras empresas vinculadas aos socios/administradores desta empresa na tabela `socios`."
            )
        if not socios:
            body += "\n\nNao encontrei registros de QSA para a empresa de origem."
        sections.append(body)

    evidence.append("Consulta empresa -> QSA/socios -> outras empresas na tabela `socios`.")
    evidence.append("Vinculo forte quando o CPF/CNPJ do socio esta disponivel; por nome pode ser apenas indicio.")
    answer = "\n\n".join(sections)
    if total_links:
        answer = (
            "Sim, encontrei empresas vinculadas por socios/administradores em comum.\n\n"
            + answer
        )
    return answer, evidence, True


def is_person_company_query(query):
    qn = _normalize(query)
    return (
        any(term in qn for term in ("tem empresa", "socio de", "socio em", "e socio", "é socio", "administrador", "representante legal"))
        or ("socio" in qn and "empresa" in qn)
    )


def is_company_profile_query(query):
    qn = _normalize(query)
    return "empresa" in qn and any(term in qn for term in (
        "todas as informacoes",
        "toda informacao",
        "informacoes disponiveis",
        "informacao disponivel",
        "dados cadastrais",
        "dados da empresa",
        "perfil da empresa",
        "quadro societario",
        "qsa",
    ))


def answer_company_profile_query(conn, query, limit=5, progress_callback=None):
    cnpj = _cnpj_digits(query)
    candidates = []
    evidence = []

    if cnpj:
        _log_query(
            progress_callback,
            "Resolver empresa por CNPJ completo em estabelecimentos + empresas",
            "SELECT ... FROM estabelecimentos est LEFT JOIN empresas e ON e.cnpj_basico = est.cnpj_basico WHERE est.cnpj_basico=? AND est.cnpj_ordem=? AND est.cnpj_dv=? LIMIT 1",
            (cnpj[:8], cnpj[8:12], cnpj[12:]),
        )
        row = _query_by_cnpj(conn, cnpj)
        if row is not None:
            candidates = [row]
        evidence.append(f"Resolucao da empresa por CNPJ `{cnpj}`.")
    else:
        name_query = _extract_name_query(query)
        if progress_callback:
            progress_callback(f"[INFO] Resolvendo empresa no CNPJ por nome: {name_query}")
        _log_query(
            progress_callback,
            "Resolver empresa por razao social/nome fantasia",
            "prefixo indexavel em empresas.razao_social; depois FTS com timeout; fallbacks com timeout",
            {
                "name": name_query,
                "limit": min(limit, 5),
                "slow_scan": ENABLE_SLOW_NAME_SCAN,
                "timeout_s": CNPJ_NAME_SEARCH_TIMEOUT_SECONDS,
            },
        )
        candidates, resolved_name = _search_company_by_literal_name(
            conn,
            name_query,
            min(limit, 5),
            progress_callback=progress_callback,
        )
        evidence.append(f"Resolucao da empresa por nome/razao social: `{resolved_name}`.")

    if not candidates:
        return (
            "Nao encontrei a empresa na base CNPJ pelo CNPJ/nome informado. "
            "Se possivel, informe o CNPJ completo para uma consulta exata."
        ), evidence, True

    sections = []
    for idx, row in enumerate(candidates[:limit], start=1):
        detail_row = row
        if row["cnpj_ordem"] and row["cnpj_dv"]:
            full_cnpj = f"{row['cnpj_basico']}{row['cnpj_ordem']}{row['cnpj_dv']}"
            _log_query(
                progress_callback,
                "Carregar cadastro completo da matriz/estabelecimento",
                "SELECT ... FROM estabelecimentos est LEFT JOIN empresas e ON e.cnpj_basico = est.cnpj_basico WHERE cnpj completo",
                full_cnpj,
            )
            detail_row = _query_by_cnpj(conn, full_cnpj) or row
        _log_query(
            progress_callback,
            "Carregar QSA/socios por cnpj_basico",
            "SELECT ... FROM socios WHERE cnpj_basico=? ORDER BY data_entrada_sociedade DESC LIMIT ?",
            (row["cnpj_basico"], 50),
        )
        socios = _query_socios(conn, row["cnpj_basico"], 50)
        section = [
            f"## Empresa {idx}",
            _format_cnpj_detail(detail_row, conn=conn),
        ]
        if socios:
            section.append("**Quadro societario / administradores:**\n\n" + _format_socios_rows(socios))
        else:
            section.append("Nao encontrei registros de socios/administradores para este CNPJ basico na tabela `socios`.")
        sections.append("\n\n".join(section))

    if len(candidates) > 1:
        intro = (
            f"Encontrei {len(candidates)} candidato(s) para a empresa informada. "
            "Abaixo estao os dados cadastrais e QSA dos principais candidatos retornados."
        )
    else:
        intro = "Encontrei o cadastro da empresa e consolidei os principais dados estruturados disponiveis."

    evidence.append("Consulta cadastral em `empresas` + `estabelecimentos` e QSA em `socios`.")
    if not ENABLE_SLOW_NAME_SCAN:
        evidence.append("Varredura lenta normalizada de nomes desativada por padrao para evitar travamentos.")
    return intro + "\n\n" + "\n\n".join(sections), evidence, True


def answer_person_company_query(conn, query, limit=20, progress_callback=None):
    if not _table_exists(conn, "socios"):
        return "A tabela `socios` nao esta disponivel no SQLite CNPJ.", [
            "Consulta por pessoa exige a tabela `socios`."
        ], False

    uf = _extract_uf(query)
    person_name = _extract_person_name_query(query)
    if not person_name or len(_company_tokens(person_name)) < 2:
        return (
            "Nao consegui identificar o nome da pessoa para pesquisar no QSA. "
            "Tente informar nome e sobrenome."
        ), ["Nome de pessoa insuficiente para consulta em `socios`."], False

    _log_query(
        progress_callback,
        "Buscar pessoa no QSA/socios e empresas vinculadas",
        "SELECT ... FROM socios s LEFT JOIN empresas e LEFT JOIN estabelecimentos est WHERE nome_socio normalizado LIKE tokens AND UF opcional LIMIT ?",
        {"person_name": person_name, "uf": uf, "limit": limit},
    )
    rows = _query_companies_by_socio_name(conn, person_name, uf=uf, limit=limit)
    evidence = [
        f"Consulta na tabela `socios` por pessoa: `{person_name}`.",
        f"Filtro UF: `{uf or 'sem filtro'}`.",
    ]
    if not rows:
        return (
            f"Nao encontrei registros de `{person_name}` como socio/administrador/representante "
            f"{'em ' + uf if uf else 'na base CNPJ carregada'}."
        ), evidence, True

    answer = (
        f"Encontrei {len(rows)} registro(s) em que `{person_name}` aparece no QSA "
        f"{'em ' + uf if uf else 'na base CNPJ'}.\n\n"
        "Abaixo estao os vinculos societarios/cadastrais retornados pela tabela `socios`, "
        "com a empresa correspondente:\n\n"
        + _format_person_company_rows(conn, rows)
    )
    return answer, evidence, True


def _execute_llm_intent(conn, query, intent):
    if not intent:
        return None, [], False

    name = intent["intent"]
    filters = intent["filters"]
    limit = intent["limit"]
    evidence = [
        f"Intencao interpretada pelo LLM: `{name}`.",
        f"Filtros validados: `{filters}`.",
    ]

    cnpj = filters.get("cnpj") or _cnpj_digits(query)
    cnpj_basico = filters.get("cnpj_basico") or (cnpj[:8] if cnpj else None)

    if name == "get_cnpj" and cnpj:
        row = _query_by_cnpj(conn, cnpj)
        if row is None:
            return "Nao encontrei esse CNPJ na base SQLite carregada.", evidence, True
        return _format_cnpj_detail(row, conn=conn), evidence, True

    if name == "get_socios" and cnpj_basico:
        socios = _query_socios(conn, cnpj_basico, limit)
        if not socios:
            return "Nao encontrei socios para esse CNPJ/CNPJ basico.", evidence, True
        answer = "Encontrei os seguintes registros no QSA da empresa:\n\n" + _format_socios_rows(socios)
        return answer, evidence, True

    if name == "search_company" and filters.get("name"):
        rows, _ = _search_by_name(conn, filters["name"], limit)
        if not rows:
            return "Nao encontrei empresa por esse nome/razao social.", evidence, True
        return _format_establishment_rows_with_cnae(conn, rows), evidence, True

    if name == "list_establishments":
        rows = _list_establishments_by_filters(conn, filters, limit)
        if not rows:
            return "Nao encontrei estabelecimentos com os filtros informados.", evidence, True
        return _format_establishment_rows_with_cnae(conn, rows), evidence, True

    if name == "count_establishments":
        total = _count_establishments_by_filters(conn, filters)
        return f"Total encontrado: **{total:,}** estabelecimento(s).".replace(",", "."), evidence, True

    if name == "count_by_uf":
        rows = _count_by_uf(conn, filters, limit)
        if not rows:
            return "Nao encontrei registros para agregar por UF.", evidence, True
        return _md_table(["UF", "Total"], [[r["uf"], r["total"]] for r in rows]), evidence, True

    return None, evidence, False


def _md_table(headers, rows):
    if not rows:
        return ""
    header_line = "| " + " | ".join(headers) + " |"
    sep_line = "| " + " | ".join("---" for _ in headers) + " |"
    data_lines = [
        "| " + " | ".join(str(value or "") for value in row) + " |"
        for row in rows
    ]
    return "\n".join([header_line, sep_line, *data_lines])


def _row_value(row, key):
    return row[key] if key in row.keys() else ""


def _format_establishment_rows(rows):
    table_rows = []
    for row in rows:
        situacao = _row_value(row, "situacao_cadastral")
        table_rows.append(
            [
                _format_cnpj(row["cnpj_basico"], row["cnpj_ordem"], row["cnpj_dv"]),
                row["razao_social"],
                row["nome_fantasia"],
                f"{situacao} - {SITUACAO_LABELS.get(situacao, '')}".strip(" -"),
                row["cnae_fiscal_principal"],
                row["uf"],
                row["municipio"],
            ]
        )
    return _md_table(
        ["CNPJ", "Razao social", "Nome fantasia", "Situacao", "CNAE", "UF", "Municipio"],
        table_rows,
    )


def _format_establishment_rows_with_cnae(conn, rows):
    table_rows = []
    for row in rows:
        situacao = _row_value(row, "situacao_cadastral")
        table_rows.append(
            [
                _format_cnpj(row["cnpj_basico"], row["cnpj_ordem"], row["cnpj_dv"]),
                row["razao_social"],
                row["nome_fantasia"],
                f"{situacao} - {SITUACAO_LABELS.get(situacao, '')}".strip(" -"),
                _format_cnae(conn, row["cnae_fiscal_principal"]),
                row["uf"],
                row["municipio"],
            ]
        )
    return _md_table(
        ["CNPJ", "Razao social", "Nome fantasia", "Situacao", "CNAE", "UF", "Municipio"],
        table_rows,
    )


def _format_socios_rows(rows):
    if not rows:
        return ""
    return _md_table(
        ["Socio/administrador", "CPF/CNPJ", "Qualificacao", "Entrada", "Representante", "Faixa etaria"],
        [
            [
                s["nome_socio_razao_social"],
                s["cpf_cnpj_socio"],
                s["qualificacao_socio"],
                s["data_entrada_sociedade"],
                s["nome_representante"],
                s["faixa_etaria"],
            ]
            for s in rows
        ],
    )


def _format_person_company_rows(conn, rows):
    if not rows:
        return ""
    table_rows = []
    for row in rows:
        situacao = row["situacao_cadastral"] or ""
        table_rows.append(
            [
                row["nome_socio_razao_social"],
                row["cpf_cnpj_socio"],
                row["qualificacao_socio"],
                row["data_entrada_sociedade"],
                _format_cnpj(row["cnpj_basico"], row["cnpj_ordem"], row["cnpj_dv"]),
                row["razao_social"],
                f"{situacao} - {SITUACAO_LABELS.get(situacao, '')}".strip(" -"),
                _format_cnae(conn, row["cnae_fiscal_principal"]),
                row["uf"],
                row["municipio"],
            ]
        )
    return _md_table(
        ["Pessoa", "CPF/CNPJ socio", "Qualificacao", "Entrada", "CNPJ empresa", "Razao social", "Situacao", "CNAE", "UF", "Municipio"],
        table_rows,
    )


def _format_cnpj_detail(row, conn=None):
    cnpj = _format_cnpj(row["cnpj_basico"], row["cnpj_ordem"], row["cnpj_dv"])
    situacao = row["situacao_cadastral"]
    address = " ".join(
        str(part or "").strip()
        for part in (
            row["tipo_logradouro"],
            row["logradouro"],
            row["numero"],
            row["complemento"],
            row["bairro"],
            row["cep"],
            row["uf"],
            row["municipio"],
        )
        if str(part or "").strip()
    )
    lines = [
        f"**CNPJ:** {cnpj}",
        f"**Razao social:** {row['razao_social'] or 'Nao informado'}",
        f"**Nome fantasia:** {row['nome_fantasia'] or 'Nao informado'}",
        f"**Situacao cadastral:** {situacao} - {SITUACAO_LABELS.get(situacao, 'Nao mapeada')}",
        f"**Data da situacao:** {row['data_situacao_cadastral'] or 'Nao informado'}",
        f"**Inicio de atividade:** {row['data_inicio_atividade'] or 'Nao informado'}",
        f"**CNAE principal:** {_format_cnae(conn, row['cnae_fiscal_principal']) if conn else (row['cnae_fiscal_principal'] or 'Nao informado')}",
        f"**Porte:** {row['porte_empresa'] or 'Nao informado'} - {PORTE_LABELS.get(row['porte_empresa'], '')}".strip(" -"),
        f"**Capital social:** {row['capital_social'] or 'Nao informado'}",
        f"**Endereco:** {address or 'Nao informado'}",
        f"**E-mail:** {row['correio_eletronico'] or 'Nao informado'}",
    ]
    return "\n\n".join(lines)


def answer_cnpj_query(
    query,
    db_path=None,
    limit=20,
    llm_model=None,
    use_llm_intent=True,
    progress_callback=None,
):
    conn, path = _connect(db_path)
    if conn is None:
        return (
            "# Resposta\n\n"
            "A pergunta parece ser sobre CNPJ, mas nao encontrei o banco SQLite de CNPJ.\n\n"
            f"Caminho esperado: `{path}`\n\n"
            "Crie a base com `tools/import_cnpj_sqlite.py` ou defina `CNPJ_SQLITE_PATH` no ambiente."
            "\n\n---\n\n# Evidencia\n\nSem banco CNPJ disponivel."
        ), [], {"strategy": "cnpj_sqlite", "db_path": str(path), "found": False}

    try:
        if not _has_any_table(conn, ("empresas", "estabelecimentos")):
            return (
                "# Resposta\n\n"
                f"O SQLite encontrado em `{path}` nao parece conter as tabelas de CNPJ esperadas."
                "\n\n---\n\n# Evidencia\n\nTabelas esperadas: empresas, estabelecimentos, socios."
            ), [], {"strategy": "cnpj_sqlite", "db_path": str(path), "found": False}

        qn = _normalize(query)
        limit = max(1, min(int(limit), 100))
        cnpj = _cnpj_digits(query)
        cnpj_basico = cnpj[:8] if cnpj else _cnpj_basico_digits(query)
        evidence = [f"Banco consultado: `{path}`"]

        if is_company_links_query(query):
            answer, links_evidence, handled = answer_company_links_query(
                conn,
                query,
                limit=limit,
                llm_model=llm_model,
                progress_callback=progress_callback,
            )
            if handled:
                evidence.extend(links_evidence)
                final_output = (
                    f"# Resposta\n\n{answer}\n\n"
                    "---\n\n"
                    "# Evidencia\n\n"
                    + "\n\n".join(f"- {item}" for item in evidence)
                )
                return final_output, [], {
                    "strategy": "cnpj_company_links",
                    "db_path": str(path),
                    "found": True,
                }

        if is_cnpj_relationship_query(query):
            answer, relation_evidence, handled = answer_relationship_query(
                conn,
                query,
                llm_model=llm_model,
                progress_callback=progress_callback,
            )
            if handled:
                evidence.extend(relation_evidence)
                final_output = (
                    f"# Resposta\n\n{answer}\n\n"
                    "---\n\n"
                    "# Evidencia\n\n"
                    + "\n\n".join(f"- {item}" for item in evidence)
                )
                return final_output, [], {
                    "strategy": "cnpj_relationship_graph",
                    "db_path": str(path),
                    "found": True,
                }

        if is_person_company_query(query):
            answer, person_evidence, handled = answer_person_company_query(
                conn,
                query,
                limit=limit,
                progress_callback=progress_callback,
            )
            if handled:
                evidence.extend(person_evidence)
                final_output = (
                    f"# Resposta\n\n{answer}\n\n"
                    "---\n\n"
                    "# Evidencia\n\n"
                    + "\n\n".join(f"- {item}" for item in evidence)
                )
                return final_output, [], {
                    "strategy": "cnpj_person_qsa",
                    "db_path": str(path),
                    "found": True,
                }

        if is_company_profile_query(query):
            answer, profile_evidence, handled = answer_company_profile_query(
                conn,
                query,
                limit=min(limit, 5),
                progress_callback=progress_callback,
            )
            if handled:
                evidence.extend(profile_evidence)
                final_output = (
                    f"# Resposta\n\n{answer}\n\n"
                    "---\n\n"
                    "# Evidencia\n\n"
                    + "\n\n".join(f"- {item}" for item in evidence)
                )
                return final_output, [], {
                    "strategy": "cnpj_company_profile",
                    "db_path": str(path),
                    "found": True,
                }

        if use_llm_intent:
            intent = _llm_cnpj_intent(
                query,
                llm_model=llm_model,
                progress_callback=progress_callback,
            )
            llm_answer, llm_evidence, handled = _execute_llm_intent(
                conn,
                query,
                intent,
            )
            if handled:
                llm_answer = _llm_consolidate_answer(
                    query,
                    llm_answer,
                    llm_model=llm_model,
                    progress_callback=progress_callback,
                )
                evidence.extend(llm_evidence)
                evidence.append("Resposta consolidada pelo LLM a partir do resultado SQLite validado.")
                final_output = (
                    f"# Resposta\n\n{llm_answer}\n\n"
                    "---\n\n"
                    "# Evidencia\n\n"
                    + "\n\n".join(f"- {item}" for item in evidence)
                )
                return final_output, [], {
                    "strategy": "cnpj_sqlite_llm_intent",
                    "db_path": str(path),
                    "found": True,
                    "intent": intent,
                }

        if cnpj:
            _log_query(
                progress_callback,
                "Consultar CNPJ completo",
                "SELECT ... FROM estabelecimentos est LEFT JOIN empresas e ON e.cnpj_basico = est.cnpj_basico WHERE cnpj completo LIMIT 1",
                (cnpj[:8], cnpj[8:12], cnpj[12:]),
            )
            row = _query_by_cnpj(conn, cnpj)
            if row is None:
                answer = "Nao encontrei esse CNPJ na base SQLite carregada."
            else:
                answer = _format_cnpj_detail(row, conn=conn)
                if "socio" in qn:
                    _log_query(
                        progress_callback,
                        "Consultar socios/QSA do CNPJ",
                        "SELECT ... FROM socios WHERE cnpj_basico=? ORDER BY data_entrada_sociedade DESC LIMIT ?",
                        (row["cnpj_basico"], limit),
                    )
                    socios = _query_socios(conn, row["cnpj_basico"], limit)
                    if socios:
                        answer += "\n\n**Socios/administradores encontrados:**\n\n"
                        answer += _format_socios_rows(socios)
                    else:
                        answer += "\n\nNao encontrei socios para esse CNPJ basico na tabela `socios`."
                evidence.append("Consulta por CNPJ completo em `estabelecimentos` + `empresas`.")

        elif cnpj_basico and "socio" in qn:
            _log_query(
                progress_callback,
                "Consultar socios/QSA por cnpj_basico",
                "SELECT ... FROM socios WHERE cnpj_basico=? ORDER BY data_entrada_sociedade DESC LIMIT ?",
                (cnpj_basico, limit),
            )
            socios = _query_socios(conn, cnpj_basico, limit)
            if socios:
                answer = "Encontrei os seguintes registros no QSA da empresa:\n\n" + _format_socios_rows(socios)
            else:
                answer = "Nao encontrei socios para esse CNPJ basico."
            evidence.append("Consulta por `cnpj_basico` na tabela `socios`.")

        elif cnpj_basico:
            _log_query(
                progress_callback,
                "Listar estabelecimentos por cnpj_basico",
                "SELECT ... FROM estabelecimentos est LEFT JOIN empresas e WHERE est.cnpj_basico=? ORDER BY est.cnpj_ordem LIMIT ?",
                (cnpj_basico, limit),
            )
            rows = _query_by_basico(conn, cnpj_basico, limit)
            answer = _format_establishment_rows_with_cnae(conn, rows) if rows else "Nao encontrei estabelecimentos para esse CNPJ basico."
            evidence.append("Consulta por `cnpj_basico` em `estabelecimentos` + `empresas`.")

        else:
            rows, filter_meta = _filter_estabelecimentos(conn, query, limit)
            if rows and filter_meta.get("aggregate") == "uf":
                answer = _md_table(["UF", "Total"], [[r["uf"], r["total"]] for r in rows])
                evidence.append(f"Consulta agregada com filtros: {filter_meta}.")
            elif rows:
                answer = _format_establishment_rows_with_cnae(conn, rows)
                evidence.append(f"Consulta filtrada em `estabelecimentos`: {filter_meta}.")
            else:
                _log_query(
                    progress_callback,
                    "Buscar empresa por nome/razao social",
                    "FTS empresas_fts/estabelecimentos_fts quando disponivel; depois LIKE controlado",
                    {"query": query, "limit": limit},
                )
                rows, name_query = _search_by_name(
                    conn,
                    query,
                    limit,
                    progress_callback=progress_callback,
                )
                if rows:
                    answer = (
                        f"Encontrei {len(rows)} candidato(s) cadastrais para a busca por nome/razao social.\n\n"
                        + _format_establishment_rows_with_cnae(conn, rows)
                    )
                    evidence.append(f"Busca textual por nome/razao social: `{name_query}`.")
                else:
                    answer = (
                        "Nao encontrei resultados na base CNPJ para essa pergunta. "
                        "Para maior precisao, informe um CNPJ completo, CNPJ basico, razao social, UF ou CNAE."
                    )
                    evidence.append("Nenhuma linha retornada pelos filtros/busca textual.")

        final_output = (
            f"# Resposta\n\n{answer}\n\n"
            "---\n\n"
            "# Evidencia\n\n"
            + "\n\n".join(f"- {item}" for item in evidence)
        )
        return final_output, [], {
            "strategy": "cnpj_sqlite",
            "db_path": str(path),
            "found": True,
        }
    finally:
        conn.close()
