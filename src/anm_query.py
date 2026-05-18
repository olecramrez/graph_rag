import json
import sre_constants
import sre_constants
import re
import sqlite3
import unicodedata
from pathlib import Path

from src.config import get_anm_db_path
from src.cnpj_query import _md_table
from src.lia_client import LIAClientError, chat_completion
from src.sql_agent import (
    answer_sql_agent_query,
    compact_schema_for_prompt,
    discover_sqlite_schema,
    extract_json_object,
    quote_identifier,
    select_relevant_schema,
)


RESOURCE_STOPWORDS = {
    "anm", "recurso", "recursos", "tabela", "tabelas", "base", "bases",
    "listar", "liste", "mostre", "mostrar", "quais", "qual", "foram",
    "foi", "sao", "são", "existem", "existe", "importado", "importados",
    "importada", "importadas", "dados", "dado", "por", "para", "com",
    "dos", "das", "uma", "uns", "nas", "nos", "total", "ranking",
    "municipio", "município", "municipios", "municípios", "valor",
}


def _normalize(text):
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", normalized.lower()).strip()


def _connect(db_path=None):
    path = Path(db_path) if db_path else get_anm_db_path()
    if not path.exists():
        return None, path
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn, path


def _table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _list_data_tables(conn, limit=50):
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type='table'
          AND name NOT LIKE 'sqlite_%'
          AND name NOT IN ('import_runs', 'datasets', 'resources', 'import_errors')
        ORDER BY name
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [row["name"] for row in rows]


def _count_table(conn, table_name):
    quoted = '"' + table_name.replace('"', '""') + '"'
    return conn.execute(f"SELECT COUNT(*) AS total FROM {quoted}").fetchone()["total"]


def _column_names(conn, table_name):
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})").fetchall()]


def _format_resources(rows):
    return _md_table(
        ["Dataset", "Recurso", "Formato", "Tabela", "Linhas", "URL"],
        [
            [
                row["dataset_title"],
                row["name"],
                row["format"],
                row["table_name"],
                row["imported_rows"],
                row["url"],
            ]
            for row in rows
        ],
    )


def _answer_overview(conn):
    datasets = conn.execute("SELECT COUNT(*) AS total FROM datasets").fetchone()["total"] if _table_exists(conn, "datasets") else 0
    resources = conn.execute("SELECT COUNT(*) AS total FROM resources").fetchone()["total"] if _table_exists(conn, "resources") else 0
    imported = 0
    if _table_exists(conn, "resources"):
        imported = conn.execute(
            "SELECT COUNT(*) AS total FROM resources WHERE table_name IS NOT NULL AND COALESCE(imported_rows, 0) > 0"
        ).fetchone()["total"]
    data_tables = _list_data_tables(conn, limit=20)
    rows = [[name, _count_table(conn, name)] for name in data_tables[:20]]
    answer = [
        f"Base ANM carregada com **{datasets}** dataset(s) e **{resources}** recurso(s).",
        f"Recursos tabulares importados: **{imported}**.",
    ]
    if rows:
        answer.append("Principais tabelas de dados encontradas:\n\n" + _md_table(["Tabela", "Linhas"], rows))
    else:
        answer.append(
            "Ainda nao encontrei tabelas de dados `anm_*`. Rode o importador sem `--metadata-only` para converter os arquivos tabulares."
        )
    return "\n\n".join(answer)


def _answer_schema(conn, limit=80):
    schema = discover_sqlite_schema(conn, max_tables=limit)
    rows = []
    for table in schema:
        columns = table.get("columns") or []
        rows.append(
            [
                table["table"],
                table.get("description", ""),
                len(columns),
                ", ".join(col["name"] for col in columns[:12]),
            ]
        )
    if not rows:
        return "Nao encontrei tabelas no SQLite ANM."
    return _md_table(["Tabela", "Descricao", "Colunas", "Primeiras colunas"], rows)


def _is_feasibility_query(query):
    qn = _normalize(query)
    patterns = (
        "e possivel",
        "seria possivel",
        "da para",
        "daria para",
        "tem como",
        "consigo",
        "conseguimos",
        "a base permite",
        "a base tem",
        "existe dado",
        "existem dados",
        "ha dados",
        "ha como",
        "pode consultar",
        "posso consultar",
    )
    return any(pattern in qn for pattern in patterns)


def _answer_query_feasibility(conn, query, limit=80, llm_model=None, progress_callback=None):
    schema = discover_sqlite_schema(conn, max_tables=limit)
    relevant_schema = select_relevant_schema(schema, query, max_tables=12)
    schema_text = compact_schema_for_prompt(relevant_schema, max_tables=12, max_columns=30)

    if progress_callback:
        tables = ", ".join(table["table"] for table in relevant_schema[:8])
        progress_callback(f"[INFO] ANM: avaliando viabilidade no schema ({tables})")

    prompt = f"""
Voce avalia se uma consulta pode ser respondida usando uma base SQLite da ANM.
Nao gere SQL executavel e nao invente tabelas ou colunas.
Use somente o schema fornecido.

Responda em portugues com:
1. Veredito: Sim, Parcialmente ou Nao.
2. Tabelas candidatas.
3. Colunas relevantes.
4. O que falta, se nao for possivel ou se for parcial.
5. Uma forma curta de perguntar a consulta executavel, se for possivel.

Schema disponivel:
{schema_text}

Pergunta do usuario:
{query}
""".strip()

    try:
        answer = chat_completion(
            [
                {
                    "role": "system",
                    "content": "Voce avalia viabilidade de consultas SQL com fidelidade estrita ao schema.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_retries=2,
            llm_model=llm_model,
        )
        evidence = [
            "Modo de viabilidade: o SQL Agent analisou o schema sem executar consulta.",
            "Tabelas consideradas: `" + "`, `".join(table["table"] for table in relevant_schema) + "`.",
        ]
        return answer, evidence
    except LIAClientError as exc:
        rows = []
        for table in relevant_schema:
            columns = table.get("columns") or []
            rows.append([
                table["table"],
                table.get("description", ""),
                ", ".join(col["name"] for col in columns[:20]),
            ])
        answer = (
            "Nao consegui acionar o LLM para avaliar a viabilidade agora.\n\n"
            "Schema mais provavel para conferir manualmente:\n\n"
            + _md_table(["Tabela", "Descricao", "Colunas"], rows)
        )
        return answer, [f"Modo de viabilidade indisponivel: {exc}"]


def _route_anm_query_with_llm(conn, query, limit=80, llm_model=None, progress_callback=None):
    schema = discover_sqlite_schema(conn, max_tables=limit)
    relevant_schema = select_relevant_schema(schema, query, max_tables=12)
    schema_text = compact_schema_for_prompt(relevant_schema, max_tables=12, max_columns=24)
    data_tables = _list_data_tables(conn, limit=40)

    prompt = f"""
Voce e o router de consultas para um SQLite da ANM.
Escolha exatamente uma rota com base na pergunta e no schema.
Nao responda a pergunta do usuario; apenas escolha a rota.

Rotas validas:
- "schema": o usuario quer estrutura, colunas, dicionario ou schema.
- "tables": o usuario quer listar tabelas importadas.
- "datasets": o usuario quer datasets/conjuntos registrados.
- "sample": o usuario quer amostra/exemplos/linhas de dados.
- "feasibility": o usuario pergunta se e possivel, se da para, se a base permite ou o que falta para realizar uma consulta.
- "resources": o usuario quer localizar recursos/arquivos/metadados, sem pedir calculo nos dados.
- "sql": o usuario pede uma consulta analitica, ranking, filtro, soma, media, comparacao, listagem de registros, agregacao ou resposta calculada a partir das tabelas.
- "overview": pedido generico sobre a base, sem objetivo consultivo claro.

Observacoes:
- Perguntas como "quais empresas arrecadam acima da media nacional" sao rota "sql".
- Perguntas de CFEM, arrecadacao, recolhimento, producao, barragens, municipio, UF, empresa, ranking, media ou total normalmente sao "sql".
- Use "resources" apenas quando a pergunta for sobre quais arquivos/recursos existem, nao quando pedir resposta calculada.

Tabelas de dados importadas:
{json.dumps(data_tables, ensure_ascii=False)}

Schema relevante:
{schema_text}

Pergunta:
{query}

Responda somente JSON:
{{
  "route": "sql",
  "confidence": "high",
  "reason": "breve justificativa"
}}
""".strip()

    raw = chat_completion(
        [
            {"role": "system", "content": "Voce escolhe rotas de consulta SQLite e responde somente JSON valido."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.0,
        max_retries=2,
        llm_model=llm_model,
    )
    payload = extract_json_object(raw)
    route = str(payload.get("route") or "overview").strip().lower()
    valid_routes = {
        "schema",
        "tables",
        "datasets",
        "sample",
        "feasibility",
        "resources",
        "sql",
        "overview",
    }
    if route not in valid_routes:
        route = "overview"

    payload["route"] = route

    if progress_callback:
        progress_callback(f"[ROUTER][ANM] rota={route}; motivo={payload.get('reason', '')}")

    return payload


def _fallback_anm_route(query):
    qn = _normalize(query)
    if any(term in qn for term in ("schema", "esquema", "estrutura", "colunas", "dicionario", "dicionario")):
        return {"route": "schema", "reason": "Fallback local: pedido explicito de schema."}
    if "tabela" in qn or "tabelas" in qn:
        return {"route": "tables", "reason": "Fallback local: pedido explicito de tabelas."}
    if any(term in qn for term in ("dataset", "datasets", "conjunto", "conjuntos")):
        return {"route": "datasets", "reason": "Fallback local: pedido explicito de datasets."}
    if any(term in qn for term in ("amostra", "exemplo", "linhas", "mostrar dados", "ver dados")):
        return {"route": "sample", "reason": "Fallback local: pedido explicito de amostra."}
    if _is_feasibility_query(query):
        return {"route": "feasibility", "reason": "Fallback local: pedido de viabilidade."}
    if _list_like_query(qn):
        return {"route": "resources", "reason": "Fallback local: pedido de recursos/metadados."}
    return {"route": "sql", "reason": "Fallback local: consulta encaminhada ao SQL Agent."}


def _list_like_query(qn):
    resource_terms = ("recurso", "recursos", "arquivo", "arquivos")
    return any(term in qn for term in resource_terms)


def _answer_imported_tables(conn, limit=80):
    data_tables = _list_data_tables(conn, limit=limit)
    data_rows = [[name, _count_table(conn, name), ", ".join(_column_names(conn, name)[:10])] for name in data_tables]
    if data_rows:
        return "Tabelas de dados importadas:\n\n" + _md_table(["Tabela", "Linhas", "Primeiras colunas"], data_rows)

    if not _table_exists(conn, "resources"):
        return "Nao encontrei a tabela `resources` para verificar importacao."

    resources = conn.execute(
        """
        SELECT
            d.title AS dataset_title,
            r.name,
            r.format,
            r.table_name,
            r.imported_rows,
            r.url
        FROM resources r
        LEFT JOIN datasets d ON d.id = r.dataset_id
        WHERE r.table_name IS NOT NULL OR COALESCE(r.imported_rows, 0) > 0
        ORDER BY d.title, r.name
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if resources:
        return "Recursos marcados como importados:\n\n" + _format_resources(resources)

    return (
        "Ainda nao ha tabelas de dados importadas no SQLite ANM.\n\n"
        "Rode o importador sem `--metadata-only`, por exemplo:\n\n"
        "`python tools\\download_anm_dados_gov_to_sqlite.py --source anm-direct --output-dir Z:\\Graph_rag\\anm_sqlite`"
    )


def _answer_datasets(conn, limit=30):
    rows = conn.execute(
        """
        SELECT title, name, organization_title, source_url
        FROM datasets
        ORDER BY title
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    if not rows:
        return "Nao encontrei datasets registrados na base ANM."
    return _md_table(
        ["Titulo", "Nome", "Organizacao", "Fonte"],
        [[row["title"], row["name"], row["organization_title"], row["source_url"]] for row in rows],
    )


def _answer_resources(conn, query, limit=30):
    qn = _normalize(query)
    params = []
    where = ""
    terms = []
    for token in re.findall(r"[a-zA-Z0-9_/-]{3,}", qn):
        if token in RESOURCE_STOPWORDS:
            continue
        terms.append(token)
    if terms:
        clauses = []
        for term in terms[:4]:
            clauses.append("(LOWER(COALESCE(r.name, '')) LIKE ? OR LOWER(COALESCE(d.title, '')) LIKE ? OR LOWER(COALESCE(r.format, '')) LIKE ?)")
            like = f"%{term}%"
            params.extend([like, like, like])
        where = "WHERE " + " AND ".join(clauses)

    rows = conn.execute(
        f"""
        SELECT
            d.title AS dataset_title,
            r.name,
            r.format,
            r.table_name,
            r.imported_rows,
            r.url
        FROM resources r
        LEFT JOIN datasets d ON d.id = r.dataset_id
        {where}
        ORDER BY d.title, r.name
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    if not rows:
        return (
            "Nao encontrei recursos ANM com esses filtros.\n\n"
            "Tente `@anm quais datasets existem?` ou `@anm quais tabelas foram importadas?` para verificar o que foi carregado."
        )
    return _format_resources(rows)


def _find_column(columns, *needles):
    normalized = [(col, _normalize(col).replace("_", " ")) for col in columns]
    for needle in needles:
        needle_norm = _normalize(needle)
        for original, norm in normalized:
            if needle_norm in norm:
                return original
    return None


def _numeric_expr(column):
    quoted = quote_identifier(column)
    return f"CAST(REPLACE(REPLACE({quoted}, '.', ''), ',', '.') AS REAL)"


def _is_cfem_query(qn):
    return any(term in qn for term in ("cfem", "arrecad", "recolh", "compensacao financeira"))


def _is_above_average_query(qn):
    return any(
        term in qn
        for term in (
            "acima da media",
            "acima da m?dia",
            "acima do medio",
            "maior que a media",
            "maior que a m?dia",
            "maiores que a media",
            "maiores que a m?dia",
            "superior a media",
            "superior a m?dia",
            "superiores a media",
            "superiores a m?dia",
        )
    )


def _select_cfem_table(tables):
    preferred = [
        "anm_cfem_cfem_arrecadacao_csv_data",
        "cfem_arrecadacao_csv_data",
    ]
    by_lower = {table.lower(): table for table in tables}
    for name in preferred:
        if name in by_lower:
            return by_lower[name]

    arrecadacao_tables = [
        table
        for table in tables
        if "arrecad" in table.lower()
        and "autuacao" not in table.lower()
        and "distribuicao" not in table.lower()
    ]
    return arrecadacao_tables[0] if arrecadacao_tables else tables[0]


def _answer_cfem_aggregate(conn, query, limit=30):
    tables = [table for table in _list_data_tables(conn, limit=300) if "cfem" in table.lower()]
    if not tables:
        return (
            "Nao encontrei tabela de dados de CFEM importada.\n\n"
            "Verifique com `@anm quais tabelas foram importadas?`. Se so houver metadados, rode o importador sem `--metadata-only`."
        ), False

    qn = _normalize(query)
    table = _select_cfem_table(tables)
    columns = _column_names(conn, table)
    group_col = None
    group_label = ""
    if "municip" in qn or "munic" in qn or "cidade" in qn:
        group_col = _find_column(columns, "municipio", "cidade")
        group_label = "Municipio"
    elif "uf" in qn or "estado" in qn:
        group_col = _find_column(columns, "uf", "sigla uf", "estado")
        group_label = "UF"
    elif "municipio" in qn or "município" in qn or "cidade" in qn:
        group_col = _find_column(columns, "municipio", "município", "cidade")
        group_label = "Municipio"
    elif "substancia" in qn or "substância" in qn or "mineral" in qn:
        group_col = _find_column(columns, "substancia", "substância", "mineral")
        group_label = "Substancia"
    elif (
        "empresa" in qn
        or "empresas" in qn
        or "cpf" in qn
        or "cnpj" in qn
    ):
        group_col = _find_column(
            columns,
            "cpf_cnpj",
            "cpf",
            "cnpj",
            "empresa"
        )
        group_label = "Empresa"
    elif "ano" in qn:
        group_col = _find_column(columns, "ano", "exercicio", "referencia")
        group_label = "Ano"

    value_col = _find_column(columns, "valor", "cfem", "arrecadacao", "arrecadação", "recolhido")
    if not group_col or not value_col:
        return (
            "Encontrei tabela CFEM, mas nao consegui identificar automaticamente coluna de agrupamento "
            f"ou valor.\n\nTabela analisada: `{table}`\n\nColunas: `{', '.join(columns[:40])}`"
        ), False

    if _is_above_average_query(qn):
        sql = (
            "WITH totais AS ("
            f"SELECT {quote_identifier(group_col)} AS grupo, "
            f"SUM({_numeric_expr(value_col)}) AS total "
            f"FROM {quote_identifier(table)} "
            f"WHERE {quote_identifier(group_col)} IS NOT NULL "
            f"AND TRIM(CAST({quote_identifier(group_col)} AS TEXT)) <> '' "
            f"GROUP BY {quote_identifier(group_col)}"
            "), media AS ("
            "SELECT AVG(total) AS media_nacional FROM totais"
            ") "
            "SELECT totais.grupo, totais.total, media.media_nacional "
            "FROM totais CROSS JOIN media "
            "WHERE totais.total > media.media_nacional "
            "ORDER BY totais.total DESC "
            f"LIMIT {int(limit)}"
        )
        rows = conn.execute(sql).fetchall()
        if not rows:
            return f"A consulta CFEM na tabela `{table}` nao retornou grupos acima da media.", True
        answer = (
            f"Empresas com arrecadacao CFEM acima da media nacional usando a tabela `{table}`. "
            "Aqui, a media nacional foi calculada como a media dos totais arrecadados por empresa no periodo disponivel:\n\n"
            + _md_table(
                [group_label, "Total", "Media nacional"],
                [[row["grupo"], row["total"], row["media_nacional"]] for row in rows],
            )
        )
        return answer, True

    sql = (
        f"SELECT {quote_identifier(group_col)} AS grupo, "
        f"SUM({_numeric_expr(value_col)}) AS total "
        f"FROM {quote_identifier(table)} "
        f"WHERE {quote_identifier(group_col)} IS NOT NULL AND TRIM(CAST({quote_identifier(group_col)} AS TEXT)) <> '' "
        f"GROUP BY {quote_identifier(group_col)} "
        "ORDER BY total DESC "
        f"LIMIT {int(limit)}"
    )
    rows = conn.execute(sql).fetchall()
    if not rows:
        return f"A consulta CFEM na tabela `{table}` nao retornou linhas.", True
    answer = (
        f"Resultado de CFEM por {group_label} usando a tabela `{table}` "
        f"e a coluna de valor `{value_col}`:\n\n"
        + _md_table([group_label, "Total"], [[row["grupo"], row["total"]] for row in rows])
    )
    return answer, True


def _answer_sample(conn, query, limit=20):
    tables = _list_data_tables(conn, limit=200)
    if not tables:
        return "Nao ha tabelas de dados importadas. Rode o importador sem `--metadata-only`."

    qn = _normalize(query)
    chosen = None
    for table in tables:
        if table.lower() in qn or any(part and part in qn for part in table.lower().split("_") if len(part) >= 4):
            chosen = table
            break
    chosen = chosen or tables[0]

    quoted = '"' + chosen.replace('"', '""') + '"'
    rows = conn.execute(f"SELECT * FROM {quoted} LIMIT ?", (limit,)).fetchall()
    if not rows:
        return f"A tabela `{chosen}` existe, mas nao retornou linhas."
    headers = rows[0].keys()
    table_rows = [[row[key] for key in headers] for row in rows]
    return f"Amostra da tabela `{chosen}`:\n\n" + _md_table(list(headers), table_rows)


def answer_anm_query(query, db_path=None, limit=30, llm_model=None, progress_callback=None):
    conn, path = _connect(db_path)
    if conn is None:
        return (
            "# Resposta\n\n"
            "Nao encontrei o SQLite da ANM.\n\n"
            f"Caminho esperado: `{path}`\n\n"
            "Crie a base com `python tools\\download_anm_dados_gov_to_sqlite.py --source anm-direct` "
            "ou defina `ANM_SQLITE_PATH` no ambiente."
            "\n\n---\n\n# Evidencia\n\nSem banco ANM disponivel."
        ), [], {"strategy": "anm_sqlite", "db_path": str(path), "found": False}

    try:
        qn = _normalize(query)
        if progress_callback:
            progress_callback(f"[QUERY][ANM] {query}")

        agent_evidence = []
        agent_meta = None

        if not _table_exists(conn, "datasets") or not _table_exists(conn, "resources"):
            answer = "O SQLite encontrado nao parece ser uma base ANM importada: faltam `datasets` e/ou `resources`."
        else:
            try:
                route_info = _route_anm_query_with_llm(
                    conn,
                    query,
                    limit=max(limit, 80),
                    llm_model=llm_model,
                    progress_callback=progress_callback,
                )
            except (LIAClientError, json.JSONDecodeError, TypeError, ValueError) as exc:
                route_info = _fallback_anm_route(query)
                agent_evidence.append(f"Router LLM indisponivel; usando fallback local: {exc}")

            route = route_info.get("route") or "overview"
            agent_evidence.append(
                f"Router ANM escolheu rota `{route}`: {route_info.get('reason', '')}"
            )

            if route == "schema":
                answer = _answer_schema(conn, limit=limit)
            elif route == "tables":
                answer = _answer_imported_tables(conn, limit=limit)
            elif route == "datasets":
                answer = _answer_datasets(conn, limit=limit)
            elif route == "sample":
                answer = _answer_sample(conn, query, limit=min(limit, 50))
            elif route == "feasibility":
                answer, feasibility_evidence = _answer_query_feasibility(
                    conn,
                    query,
                    limit=max(limit, 80),
                    llm_model=llm_model,
                    progress_callback=progress_callback,
                )
                agent_evidence.extend(feasibility_evidence)
            elif route == "resources":
                answer = _answer_resources(conn, query, limit=limit)
            elif route == "sql":
                if _list_data_tables(conn, limit=1):
                    try:
                        answer, sql_evidence, agent_meta = answer_sql_agent_query(
                            conn,
                            query,
                            llm_model=llm_model,
                            limit=max(limit, 100),
                            progress_callback=progress_callback,
                        )
                        agent_evidence.extend(sql_evidence or [])
                    except Exception as exc:
                        answer = (
                            "O router LLM classificou esta pergunta como consulta analitica SQL, "
                            "mas o SQL Agent nao conseguiu gerar/executar a consulta.\n\n"
                            f"Motivo: `{exc}`\n\n"
                            "Use `@anm esquema da base` ou "
                            "`@anm quais tabelas foram importadas?` "
                            "para conferir os nomes de tabelas e colunas."
                        )
                        agent_evidence.append(f"SQL Agent indisponivel para rota `sql`: {exc}")
                else:
                    answer = _answer_overview(conn)
                    agent_evidence.append("Router escolheu `sql`, mas nao ha tabelas de dados importadas.")
            else:
                answer = _answer_overview(conn)

        evidence_items = [
            f"- Banco consultado: `{path}`",
            "- Consulta via comando explicito `@anm`.",
        ]
        evidence_items.extend(f"- {item}" for item in agent_evidence)

        final_output = (
            f"# Resposta\n\n{answer}\n\n"
            "---\n\n"
            "# Evidencia\n\n"
            + "\n\n".join(evidence_items)
        )
        routing = {"strategy": "anm_sqlite", "db_path": str(path), "found": True}
        if agent_meta:
            routing["sql_agent"] = agent_meta
        return final_output, [], routing
    finally:
        conn.close()
