import re
import sqlite3
import unicodedata
from pathlib import Path

from src.config import get_anm_db_path
from src.cnpj_query import _md_table
from src.sql_agent import answer_sql_agent_query, discover_sqlite_schema, quote_identifier


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


def _answer_cfem_aggregate(conn, query, limit=30):
    tables = [table for table in _list_data_tables(conn, limit=300) if "cfem" in table.lower()]
    if not tables:
        return (
            "Nao encontrei tabela de dados de CFEM importada.\n\n"
            "Verifique com `@anm quais tabelas foram importadas?`. Se so houver metadados, rode o importador sem `--metadata-only`."
        ), False

    qn = _normalize(query)
    table = tables[0]
    columns = _column_names(conn, table)
    group_col = None
    group_label = ""
    if "municip" in qn or "munic" in qn or "cidade" in qn:
        group_col = _find_column(columns, "municipio", "cidade")
        group_label = "Municipio"
    elif "uf" in qn:
        group_col = _find_column(columns, "uf", "sigla uf", "estado")
        group_label = "UF"
    elif "municipio" in qn or "município" in qn or "cidade" in qn:
        group_col = _find_column(columns, "municipio", "município", "cidade")
        group_label = "Municipio"
    elif "substancia" in qn or "substância" in qn or "mineral" in qn:
        group_col = _find_column(columns, "substancia", "substância", "mineral")
        group_label = "Substancia"
    elif "ano" in qn:
        group_col = _find_column(columns, "ano", "exercicio", "referencia")
        group_label = "Ano"

    value_col = _find_column(columns, "valor", "cfem", "arrecadacao", "arrecadação", "recolhido")
    if not group_col or not value_col:
        return (
            "Encontrei tabela CFEM, mas nao consegui identificar automaticamente coluna de agrupamento "
            f"ou valor.\n\nTabela analisada: `{table}`\n\nColunas: `{', '.join(columns[:40])}`"
        ), False

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
        elif any(term in qn for term in ("schema", "esquema", "estrutura", "colunas", "dicionario", "dicionário")):
            answer = _answer_schema(conn, limit=limit)
        elif "tabela" in qn or "tabelas" in qn:
            answer = _answer_imported_tables(conn, limit=limit)
        elif any(term in qn for term in ("dataset", "datasets", "conjunto", "conjuntos")):
            answer = _answer_datasets(conn, limit=limit)
        elif any(term in qn for term in ("amostra", "exemplo", "linhas", "mostrar dados", "ver dados")):
            answer = _answer_sample(conn, query, limit=min(limit, 50))
        elif any(term in qn for term in ("recurso", "recursos", "tabela", "tabelas", "cfem", "amb", "dipem", "barragem")):
            if "cfem" in qn and any(term in qn for term in ("total", "soma", "por ", "ranking", "maior", "menor")):
                answer, handled_cfem = _answer_cfem_aggregate(conn, query, limit=limit)
                if not handled_cfem:
                    agent_evidence = ["Consulta CFEM deterministica nao encontrou colunas suficientes."]
            elif any(term in qn for term in ("quantos", "total", "soma", "media", "média", "maior", "menor", "por ", "ranking", "listar")) and _list_data_tables(conn, limit=1):
                try:
                    answer, agent_evidence, agent_meta = answer_sql_agent_query(
                        conn,
                        query,
                        llm_model=llm_model,
                        limit=max(limit, 100),
                        progress_callback=progress_callback,
                    )
                except Exception as exc:
                    answer = (
                        "O SQL Agent nao conseguiu gerar/executar a consulta estruturada.\n\n"
                        f"Motivo: `{exc}`\n\n"
                        "Use `@anm esquema da base` ou `@anm quais tabelas foram importadas?` para conferir nomes de tabelas e colunas."
                    )
                    agent_evidence = [f"SQL Agent indisponivel; fallback para metadados: {exc}"]
            else:
                answer = _answer_resources(conn, query, limit=limit)
        else:
            if _list_data_tables(conn, limit=1):
                try:
                    answer, agent_evidence, agent_meta = answer_sql_agent_query(
                        conn,
                        query,
                        llm_model=llm_model,
                        limit=max(limit, 100),
                        progress_callback=progress_callback,
                    )
                except Exception as exc:
                    answer = _answer_overview(conn)
                    agent_evidence = [f"SQL Agent indisponivel; fallback para visao geral: {exc}"]
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
