import re
import sqlite3
from pathlib import Path

from src.config import get_anm_db_path
from src.cnpj_query import _md_table


def _normalize(text):
    return re.sub(r"\s+", " ", str(text or "").lower()).strip()


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
        if token in {"recurso", "recursos", "tabela", "tabelas", "base", "bases", "listar", "mostre", "quais", "anm"}:
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
        return "Nao encontrei recursos ANM com esses filtros."
    return _format_resources(rows)


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


def answer_anm_query(query, db_path=None, limit=30, progress_callback=None):
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

        if not _table_exists(conn, "datasets") or not _table_exists(conn, "resources"):
            answer = "O SQLite encontrado nao parece ser uma base ANM importada: faltam `datasets` e/ou `resources`."
        elif any(term in qn for term in ("dataset", "datasets", "conjunto", "conjuntos")):
            answer = _answer_datasets(conn, limit=limit)
        elif any(term in qn for term in ("amostra", "exemplo", "linhas", "mostrar dados", "ver dados")):
            answer = _answer_sample(conn, query, limit=min(limit, 50))
        elif any(term in qn for term in ("recurso", "recursos", "tabela", "tabelas", "cfem", "amb", "dipem", "barragem")):
            answer = _answer_resources(conn, query, limit=limit)
        else:
            answer = _answer_overview(conn)

        final_output = (
            f"# Resposta\n\n{answer}\n\n"
            "---\n\n"
            "# Evidencia\n\n"
            f"- Banco consultado: `{path}`\n\n"
            "- Consulta via comando explicito `@anm`."
        )
        return final_output, [], {"strategy": "anm_sqlite", "db_path": str(path), "found": True}
    finally:
        conn.close()
