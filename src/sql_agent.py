import json
import re
import sqlite3
import time

from src.cnpj_query import _md_table
from src.lia_client import LIAClientError, chat_completion


BLOCKED_SQL_RE = re.compile(
    r"\b(drop|delete|update|insert|alter|attach|detach|pragma|vacuum|replace|create|reindex|analyze)\b",
    re.IGNORECASE,
)
ALLOWED_SQL_RE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE | re.DOTALL)
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


ENTITY_PATTERNS = {
    "cnpj": re.compile(r"\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b"),
    "cpf": re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),
    "processo": re.compile(r"\b\d{4,7}[./-]\d{2,4}(?:[./-]\d+)?\b"),
    "barragem": re.compile(r"\bbarragem|barragens\b", re.IGNORECASE),
    "cfem": re.compile(r"\bcfem|compensacao financeira|compensação financeira\b", re.IGNORECASE),
    "anm": re.compile(r"\banm|agencia nacional de mineracao|agência nacional de mineração\b", re.IGNORECASE),
    "anp": re.compile(r"\banp|agencia nacional do petroleo|agência nacional do petróleo\b", re.IGNORECASE),
}

ENTITY_TERMS = {
    "empresa": ("empresa", "razao social", "razão social", "titular", "empreendedor"),
    "socio": ("socio", "sócio", "socios", "sócios"),
    "administrador": ("administrador", "administradora"),
    "representante": ("representante legal", "representante"),
    "municipio": ("municipio", "município", "cidade"),
    "substancia": ("substancia", "substância", "mineral", "minerio", "minério"),
}


def quote_identifier(name):
    if not IDENTIFIER_RE.match(str(name or "")):
        raise ValueError(f"Identificador SQL invalido: {name}")
    return '"' + name.replace('"', '""') + '"'


def extract_json_object(text):
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json|sql)?", "", raw, flags=re.IGNORECASE).strip()
        raw = re.sub(r"```$", "", raw).strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        return json.loads(raw[start:end + 1])
    return {"sql": raw}


def normalize_text(text):
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


def discover_sqlite_schema(conn, sample_values=False, max_tables=80, max_columns=80):
    tables = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type IN ('table', 'view')
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        LIMIT ?
        """,
        (max_tables,),
    ).fetchall()
    schema = []
    for table_row in tables:
        table = table_row["name"] if isinstance(table_row, sqlite3.Row) else table_row[0]
        columns = []
        for col in conn.execute(f"PRAGMA table_info({quote_identifier(table)})").fetchmany(max_columns):
            col_name = col["name"] if isinstance(col, sqlite3.Row) else col[1]
            col_type = col["type"] if isinstance(col, sqlite3.Row) else col[2]
            columns.append(
                {
                    "name": col_name,
                    "type": col_type or "TEXT",
                    "description": infer_column_description(col_name),
                    "aliases": infer_aliases(col_name),
                }
            )
        schema.append(
            {
                "table": table,
                "description": infer_table_description(table),
                "columns": columns,
                "aliases": infer_aliases(table),
                "relations": [],
            }
        )
    return schema


def infer_table_description(table):
    name = normalize_text(table).replace("_", " ")
    if "resources" == table:
        return "Metadados dos recursos/arquivos importados."
    if "datasets" == table:
        return "Metadados dos conjuntos de dados importados."
    if "cfem" in name:
        return "Dados relacionados a CFEM/arrecadacao mineral."
    if "barragem" in name:
        return "Dados relacionados a barragens de mineracao."
    if "amb" in name:
        return "Dados do Anuario Mineral Brasileiro."
    if "dipem" in name:
        return "Dados DIPEM/processos e direitos minerarios."
    return f"Tabela importada: {table}."


def infer_column_description(column):
    name = normalize_text(column).replace("_", " ")
    hints = [
        ("cnpj", "CNPJ ou identificador de pessoa juridica."),
        ("cpf", "CPF ou identificador de pessoa fisica."),
        ("razao", "Razao social ou nome empresarial."),
        ("empresa", "Nome ou identificador de empresa."),
        ("titular", "Titular ou responsavel pelo direito/processo."),
        ("municipio", "Municipio."),
        ("uf", "Unidade da federacao."),
        ("substancia", "Substancia mineral."),
        ("processo", "Numero ou identificador de processo."),
        ("barragem", "Nome ou identificador de barragem."),
        ("valor", "Valor numerico/financeiro."),
        ("ano", "Ano de referencia."),
        ("data", "Data."),
    ]
    for token, description in hints:
        if token in name:
            return description
    return f"Coluna {column}."


def infer_aliases(name):
    text = normalize_text(name).replace("_", " ")
    aliases = set(filter(None, [text]))
    alias_map = {
        "razao": ["razao social", "razão social", "empresa", "nome empresarial"],
        "cnpj": ["cnpj", "empresa"],
        "cpf": ["cpf", "pessoa fisica", "pessoa física"],
        "municipio": ["municipio", "município", "cidade"],
        "substancia": ["substancia", "substância", "mineral"],
        "cfem": ["cfem", "arrecadacao", "arrecadação"],
        "barragem": ["barragem", "barragens"],
        "processo": ["processo", "processo minerario", "processo minerário"],
    }
    for token, values in alias_map.items():
        if token in text:
            aliases.update(values)
    return sorted(aliases)


def detect_entities(query):
    text = str(query or "")
    lowered = normalize_text(text)
    entities = {}
    for name, pattern in ENTITY_PATTERNS.items():
        values = [match.group(0) for match in pattern.finditer(text)]
        if values:
            entities[name] = values
    for name, terms in ENTITY_TERMS.items():
        if any(term in lowered for term in terms):
            entities.setdefault(name, []).append(name)
    return entities


def classify_sql_question(query, entities=None):
    qn = normalize_text(query)
    entities = entities or detect_entities(query)
    if any(term in qn for term in ("documento", "norma", "portaria", "resolucao", "parecer", "ata", "texto")):
        if any(term in qn for term in ("quantos", "total", "listar", "tabela", "valor", "municipio", "uf")):
            return "hybrid_sql_rag"
        return "rag_only"
    if any(term in qn for term in ("relacao", "relação", "vinculo", "vínculo", "socio", "sócio", "grupo economico")):
        return "graph_only"
    if entities or any(term in qn for term in ("quantos", "total", "listar", "maior", "menor", "soma", "media", "média", "por uf", "por municipio", "por município")):
        return "sql_only"
    return "sql_only"


def compact_schema_for_prompt(schema, max_tables=30, max_columns=24):
    lines = []
    for table in schema[:max_tables]:
        columns = table.get("columns") or []
        col_text = ", ".join(
            f"{col['name']} {col.get('type') or ''}".strip()
            for col in columns[:max_columns]
        )
        lines.append(
            f"- {table['table']}: {table.get('description', '')} Colunas: {col_text}"
        )
    return "\n".join(lines)


def select_relevant_schema(schema, query, max_tables=18):
    qn = normalize_text(query)
    scored = []
    for table in schema:
        score = 0
        haystack = " ".join(
            [
                table.get("table", ""),
                table.get("description", ""),
                " ".join(table.get("aliases") or []),
                " ".join(
                    " ".join([col.get("name", ""), col.get("description", ""), " ".join(col.get("aliases") or [])])
                    for col in (table.get("columns") or [])
                ),
            ]
        )
        normalized = normalize_text(haystack)
        for token in set(re.findall(r"[a-zA-Z0-9_/-]{3,}", qn)):
            if token in normalized:
                score += 2
        if table.get("table", "").lower() in {"datasets", "resources"}:
            score += 1
        scored.append((score, table))
    scored.sort(key=lambda item: item[0], reverse=True)
    selected = [table for score, table in scored if score > 0][:max_tables]
    return selected or [table for _, table in scored[:max_tables]]


def generate_sql_with_llm(query, schema, entities=None, llm_model=None):
    schema_text = compact_schema_for_prompt(schema)
    prompt = f"""
Voce e um gerador de SQL SQLite para uma base analitica.
Responda somente JSON puro, sem markdown.

Regras obrigatorias:
- Gere apenas uma consulta SELECT ou WITH.
- Nunca use DROP, DELETE, UPDATE, INSERT, ALTER, ATTACH, DETACH, PRAGMA, VACUUM, CREATE.
- Use somente tabelas e colunas presentes no schema.
- Inclua LIMIT se a consulta puder retornar muitas linhas.
- Prefira agregacoes para perguntas de contagem, ranking, soma, media e agrupamento.
- Nao invente tabelas nem colunas.

Schema disponivel:
{schema_text}

Entidades detectadas:
{json.dumps(entities or {}, ensure_ascii=False)}

Pergunta:
{query}

Formato:
{{
  "sql": "SELECT ...",
  "reason": "breve justificativa",
  "mode": "sql_only"
}}
""".strip()

    raw = chat_completion(
        [
            {"role": "system", "content": "Voce gera SQL SQLite seguro e responde apenas JSON valido."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_retries=2,
        llm_model=llm_model,
    )
    payload = extract_json_object(raw)
    sql = str(payload.get("sql") or "").strip()
    reason = str(payload.get("reason") or "SQL gerado pelo LLM.").strip()
    return sql, reason


def validate_sql(sql):
    normalized = str(sql or "").strip().rstrip(";")
    if not normalized:
        raise ValueError("SQL vazio.")
    if ";" in normalized:
        raise ValueError("Apenas uma instrucao SQL e permitida.")
    if not ALLOWED_SQL_RE.match(normalized):
        raise ValueError("Apenas SELECT ou WITH sao permitidos.")
    if BLOCKED_SQL_RE.search(normalized):
        raise ValueError("SQL contem palavra bloqueada.")
    return normalized


def apply_limit(sql, limit):
    if re.search(r"\blimit\s+\d+\b", sql, flags=re.IGNORECASE):
        return sql
    return f"{sql}\nLIMIT {int(limit)}"


def execute_safe_sql(conn, sql, limit=100, timeout_seconds=8):
    safe_sql = apply_limit(validate_sql(sql), limit)
    deadline = time.monotonic() + max(1.0, float(timeout_seconds or 8))

    def stop_if_expired():
        return 1 if time.monotonic() > deadline else 0

    def authorizer(action, arg1, arg2, dbname, source):
        allowed = {
            sqlite3.SQLITE_SELECT,
            sqlite3.SQLITE_READ,
            sqlite3.SQLITE_FUNCTION,
        }
        if hasattr(sqlite3, "SQLITE_RECURSIVE"):
            allowed.add(sqlite3.SQLITE_RECURSIVE)
        return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY

    try:
        conn.set_progress_handler(stop_if_expired, 1000)
        conn.set_authorizer(authorizer)
        rows = conn.execute(safe_sql).fetchmany(limit)
        return safe_sql, rows
    except sqlite3.DatabaseError as exc:
        if "interrupted" in str(exc).lower():
            raise TimeoutError(f"Consulta SQL excedeu {timeout_seconds:.1f}s.") from exc
        raise
    finally:
        conn.set_authorizer(None)
        conn.set_progress_handler(None, 0)


def rows_to_markdown(rows, max_cell_chars=300):
    if not rows:
        return "A consulta nao retornou linhas."
    headers = list(rows[0].keys())
    table_rows = []
    for row in rows:
        values = []
        for header in headers:
            value = row[header]
            text = "" if value is None else str(value)
            if len(text) > max_cell_chars:
                text = text[: max_cell_chars - 3] + "..."
            values.append(text)
        table_rows.append(values)
    return _md_table(headers, table_rows)


def consolidate_sql_answer(query, sql, rows_markdown, llm_model=None):
    prompt = f"""
Pergunta do usuario:
{query}

SQL executado:
{sql}

Resultado tabular:
{rows_markdown[:12000]}

Redija uma resposta objetiva em portugues.
Use somente os dados fornecidos. Nao invente valores.
Se houver tabela, preserve uma tabela Markdown quando for util.
""".strip()
    try:
        return chat_completion(
            [
                {"role": "system", "content": "Voce consolida resultados SQL com fidelidade aos dados."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_retries=2,
            llm_model=llm_model,
        )
    except LIAClientError:
        return rows_markdown


def answer_sql_agent_query(
    conn,
    query,
    llm_model=None,
    limit=100,
    timeout_seconds=8,
    progress_callback=None,
    consolidate=True,
):
    schema = discover_sqlite_schema(conn)
    entities = detect_entities(query)
    mode = classify_sql_question(query, entities)
    relevant_schema = select_relevant_schema(schema, query)

    if progress_callback:
        progress_callback(f"[INFO] SQL Agent: modo={mode}, entidades={entities or {}}")

    try:
        sql, reason = generate_sql_with_llm(
            query,
            relevant_schema,
            entities=entities,
            llm_model=llm_model,
        )
    except (LIAClientError, ValueError, json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(f"Falha ao gerar SQL com LLM: {exc}") from exc

    safe_sql, rows = execute_safe_sql(
        conn,
        sql,
        limit=limit,
        timeout_seconds=timeout_seconds,
    )
    markdown = rows_to_markdown(rows)
    answer = consolidate_sql_answer(query, safe_sql, markdown, llm_model=llm_model) if consolidate else markdown
    evidence = [
        f"Modo classificado: `{mode}`.",
        f"Entidades detectadas: `{json.dumps(entities, ensure_ascii=False)}`.",
        f"SQL executado: `{safe_sql}`.",
        f"Justificativa SQL: {reason}",
    ]
    return answer, evidence, {
        "mode": mode,
        "sql": safe_sql,
        "rows": len(rows),
        "schema_tables": [table["table"] for table in relevant_schema],
    }
