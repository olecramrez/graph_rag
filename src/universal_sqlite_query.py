import json
import re
import sqlite3
import unicodedata
from pathlib import Path

from src.lia_client import LIAClientError, chat_completion
from src.sql_agent import (
    answer_sql_agent_query,
    compact_schema_for_prompt,
    discover_sqlite_schema,
    extract_json_object,
    quote_identifier,
    select_relevant_schema,
)
from src.sqlite_schema_library import load_or_build_schema_profile
from src.utils_table import _md_table


def _normalize(text):
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    normalized = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", normalized.lower()).strip()


def _connect(db_path):
    path = Path(db_path) if db_path else None
    if not path or not path.exists():
        return None, path
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-100000")
        conn.execute("PRAGMA query_only=ON")
    except sqlite3.Error:
        pass
    return conn, path


def _table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _list_tables(conn, limit=80):
    rows = conn.execute(
        """
        SELECT name, type
        FROM sqlite_master
        WHERE type IN ('table','view')
          AND name NOT LIKE 'sqlite_%'
        ORDER BY name
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def _count_table(conn, table_name):
    return conn.execute(f"SELECT COUNT(*) AS total FROM {quote_identifier(table_name)}").fetchone()["total"]


def _column_names(conn, table_name):
    return [
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({quote_identifier(table_name)})").fetchall()
    ]


def _answer_overview(conn, db_path, limit=30):
    tables = _list_tables(conn, limit=limit)
    rows = []
    for item in tables:
        name = item["name"]
        try:
            total = _count_table(conn, name) if item["type"] == "table" else ""
        except sqlite3.Error:
            total = ""
        rows.append([name, item["type"], total, ", ".join(_column_names(conn, name)[:10])])

    if not rows:
        return f"O SQLite `{db_path}` nao possui tabelas ou views consultaveis."

    return (
        f"Base SQLite conectada: `{db_path}`.\n\n"
        + _md_table(["Tabela/View", "Tipo", "Linhas", "Primeiras colunas"], rows)
    )


def _get_schema(conn, db_path=None, limit=80, base_name=None, progress_callback=None):
    if db_path:
        try:
            profile, rebuilt = load_or_build_schema_profile(db_path, base_name=base_name)
            schema = profile.get("schema") or []
            if progress_callback:
                source = "recriada" if rebuilt else "biblioteca"
                progress_callback(f"[INFO] Schema SQLite carregado da {source}: {profile.get('db_name')}")
            return schema[:limit]
        except Exception as exc:
            if progress_callback:
                progress_callback(f"[WARN] Biblioteca de schema indisponivel; lendo SQLite diretamente: {exc}")
    return discover_sqlite_schema(conn, max_tables=limit)


def _answer_schema(conn, limit=80, db_path=None, base_name=None, progress_callback=None):
    schema = _get_schema(conn, db_path=db_path, limit=limit, base_name=base_name, progress_callback=progress_callback)
    rows = []
    for table in schema:
        columns = table.get("columns") or []
        rows.append(
            [
                table["table"],
                table.get("description", ""),
                len(columns),
                ", ".join(col["name"] for col in columns[:16]),
            ]
        )
    if not rows:
        return "Nao encontrei tabelas no SQLite selecionado."
    return _md_table(["Tabela", "Descricao", "Colunas", "Primeiras colunas"], rows)


def _answer_tables(conn, limit=80):
    rows = []
    for item in _list_tables(conn, limit=limit):
        name = item["name"]
        try:
            total = _count_table(conn, name) if item["type"] == "table" else ""
        except sqlite3.Error:
            total = ""
        rows.append([name, item["type"], total])
    if not rows:
        return "Nao encontrei tabelas/views no SQLite selecionado."
    return _md_table(["Nome", "Tipo", "Linhas"], rows)


def _answer_sample(conn, query, limit=20):
    tables = [item["name"] for item in _list_tables(conn, limit=200)]
    if not tables:
        return "Nao ha tabelas/views consultaveis no SQLite selecionado."

    qn = _normalize(query)
    chosen = None
    for table in tables:
        table_norm = _normalize(table).replace("_", " ")
        if table.lower() in qn or any(part and part in qn for part in table_norm.split() if len(part) >= 4):
            chosen = table
            break
    chosen = chosen or tables[0]

    rows = conn.execute(f"SELECT * FROM {quote_identifier(chosen)} LIMIT ?", (limit,)).fetchall()
    if not rows:
        return f"A tabela `{chosen}` existe, mas nao retornou linhas."
    headers = rows[0].keys()
    return f"Amostra da tabela `{chosen}`:\n\n" + _md_table(
        list(headers),
        [[row[key] for key in headers] for row in rows],
    )


def _dictionary_score(query, table, column):
    qn = _normalize(query)
    haystack = " ".join(
        [
            table.get("table", ""),
            table.get("description", ""),
            column.get("name", ""),
            column.get("description", ""),
            " ".join(column.get("aliases") or []),
            " ".join(f"{key} {value}" for key, value in (column.get("value_map") or {}).items()),
        ]
    )
    hn = _normalize(haystack).replace("_", "")
    score = 0
    column_name = _normalize(column.get("name")).replace("_", "")
    if column_name and column_name in qn.replace("_", ""):
        score += 12
    for token in set(re.findall(r"[a-zA-Z0-9_/-]{2,}", qn)):
        if token in hn:
            score += 2
    if column.get("value_map"):
        score += 4
    return score


def _answer_dictionary(conn, query, limit=80, db_path=None, base_name=None, progress_callback=None):
    schema = _get_schema(conn, db_path=db_path, limit=limit, base_name=base_name, progress_callback=progress_callback)
    qn = _normalize(query)
    requested_codes = set(re.findall(r"\b\d+\b", qn))
    matches = []

    for table in schema:
        if table.get("table") in {"import_files"}:
            continue
        for column in table.get("columns") or []:
            score = _dictionary_score(query, table, column)
            if score <= 0:
                continue
            value_map = column.get("value_map") or {}
            matched_codes = [code for code in requested_codes if code in value_map]
            if requested_codes and not matched_codes:
                continue
            matches.append((score, table, column, matched_codes))

    matches.sort(key=lambda item: item[0], reverse=True)
    if not matches:
        return (
            "Nao encontrei esse campo/codigo no dicionario incorporado ao schema.\n\n"
            "Tente perguntar pelo nome exato do campo, por exemplo: "
            "`@sqlite o que significa IdeMotivoExpurgo codigo 0?`"
        ), ["Consulta ao dicionario do schema nao encontrou correspondencia."]

    rows = []
    for _, table, column, matched_codes in matches[:10]:
        value_map = column.get("value_map") or {}
        if matched_codes:
            codes_text = "; ".join(f"{code} = {value_map[code]}" for code in matched_codes)
        elif value_map:
            codes_text = "; ".join(f"{code} = {label}" for code, label in list(value_map.items())[:12])
        else:
            codes_text = ""
        rows.append(
            [
                table.get("table", ""),
                column.get("name", ""),
                column.get("dictionary_type") or column.get("type") or "",
                column.get("description", ""),
                codes_text,
            ]
        )

    answer = _md_table(["Tabela", "Campo", "Tipo", "Descricao", "Codigos"], rows)
    return answer, ["Resposta obtida do dicionario incorporado ao schema SQLite."]


def _is_feasibility_query(query):
    qn = _normalize(query)
    return any(
        pattern in qn
        for pattern in (
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
        )
    )


def _answer_feasibility(conn, query, limit=80, llm_model=None, progress_callback=None, db_path=None, base_name=None):
    schema = _get_schema(conn, db_path=db_path, limit=limit, base_name=base_name, progress_callback=progress_callback)
    relevant_schema = select_relevant_schema(schema, query, max_tables=12)
    schema_text = compact_schema_for_prompt(relevant_schema, max_tables=12, max_columns=30)

    if progress_callback:
        progress_callback("[INFO] SQLite universal: avaliando viabilidade no schema selecionado")

    prompt = f"""
Voce avalia se uma consulta pode ser respondida usando uma base SQLite.
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

    answer = chat_completion(
        [
            {"role": "system", "content": "Voce avalia viabilidade de consultas SQL com fidelidade estrita ao schema."},
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


def _route_sqlite_query_with_llm(conn, query, limit=80, llm_model=None, progress_callback=None, db_path=None, base_name=None):
    schema = _get_schema(conn, db_path=db_path, limit=limit, base_name=base_name, progress_callback=progress_callback)
    relevant_schema = select_relevant_schema(schema, query, max_tables=12)
    schema_text = compact_schema_for_prompt(relevant_schema, max_tables=12, max_columns=24)
    tables = [item["name"] for item in _list_tables(conn, limit=80)]

    prompt = f"""
Voce e o router de consultas para um SQLite generico.
Escolha exatamente uma rota com base na pergunta e no schema.
Nao responda a pergunta do usuario; apenas escolha a rota.

Rotas validas:
- "schema": o usuario quer estrutura, colunas, dicionario ou schema.
- "dictionary": o usuario pergunta o significado de um campo, coluna, codigo, valor enumerado ou item do dicionario de dados.
- "tables": o usuario quer listar tabelas ou views.
- "sample": o usuario quer amostra/exemplos/linhas de dados.
- "feasibility": o usuario pergunta se e possivel, se da para, se a base permite ou o que falta para realizar uma consulta.
- "sql": o usuario pede consulta analitica, ranking, filtro, soma, media, comparacao, listagem de registros, agregacao ou resposta calculada.
- "overview": pedido generico sobre a base, sem objetivo consultivo claro.

Use "dictionary" para perguntas como "o que e o campo X", "o que significa codigo 0 em X", "traduza o dicionario" ou "qual significado de X numero 0".

Tabelas/views disponiveis:
{json.dumps(tables, ensure_ascii=False)}

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
    valid_routes = {"schema", "dictionary", "tables", "sample", "feasibility", "sql", "overview"}
    if route not in valid_routes:
        route = "overview"
    payload["route"] = route

    if progress_callback:
        progress_callback(f"[ROUTER][SQLITE] rota={route}; motivo={payload.get('reason', '')}")

    return payload


def _fallback_route(query):
    qn = _normalize(query)
    if any(term in qn for term in ("o que", "significa", "significado", "codigo", "numero")):
        return {"route": "dictionary", "reason": "Fallback local: pergunta sobre significado/codigo."}
    if any(term in qn for term in ("schema", "esquema", "estrutura", "colunas", "dicionario")):
        if any(term in qn for term in ("o que", "significa", "significado", "codigo", "código", "numero", "número", "traduz")):
            return {"route": "dictionary", "reason": "Fallback local: pergunta sobre significado no dicionario."}
        return {"route": "schema", "reason": "Fallback local: pedido explicito de schema."}
    if any(term in qn for term in ("o que e", "o que é", "significa", "significado", "codigo", "código")):
        return {"route": "dictionary", "reason": "Fallback local: pergunta sobre significado/codigo."}
    if "tabela" in qn or "tabelas" in qn or "views" in qn:
        return {"route": "tables", "reason": "Fallback local: pedido explicito de tabelas."}
    if any(term in qn for term in ("amostra", "exemplo", "linhas", "mostrar dados", "ver dados")):
        return {"route": "sample", "reason": "Fallback local: pedido explicito de amostra."}
    if _is_feasibility_query(query):
        return {"route": "feasibility", "reason": "Fallback local: pedido de viabilidade."}
    return {"route": "sql", "reason": "Fallback local: consulta encaminhada ao SQL Agent."}


def answer_universal_sqlite_query(
    query,
    db_path,
    limit=30,
    llm_model=None,
    progress_callback=None,
    label=None,
):
    conn, path = _connect(db_path)
    if conn is None:
        expected = str(path) if path else "nenhum caminho selecionado"
        return (
            "# Resposta\n\n"
            "Nao encontrei o SQLite selecionado.\n\n"
            f"Caminho: `{expected}`"
            "\n\n---\n\n# Evidencia\n\nSem banco SQLite disponivel."
        ), [], {"strategy": "sqlite_universal", "db_path": expected, "found": False}

    try:
        if progress_callback:
            progress_callback(f"[QUERY][SQLITE] {query}")

        agent_evidence = []
        agent_meta = None

        if not _list_tables(conn, limit=1):
            answer = "O SQLite selecionado nao possui tabelas ou views consultaveis."
        else:
            try:
                route_info = _route_sqlite_query_with_llm(
                    conn,
                    query,
                    limit=max(limit, 80),
                    llm_model=llm_model,
                    progress_callback=progress_callback,
                    db_path=path,
                    base_name=label,
                )
            except (LIAClientError, json.JSONDecodeError, TypeError, ValueError) as exc:
                route_info = _fallback_route(query)
                agent_evidence.append(f"Router LLM indisponivel; usando fallback local: {exc}")

            route = route_info.get("route") or "overview"
            agent_evidence.append(f"Router SQLite escolheu rota `{route}`: {route_info.get('reason', '')}")

            if route == "schema":
                answer = _answer_schema(
                    conn,
                    limit=limit,
                    db_path=path,
                    base_name=label,
                    progress_callback=progress_callback,
                )
            elif route == "dictionary":
                answer, dictionary_evidence = _answer_dictionary(
                    conn,
                    query,
                    limit=max(limit, 80),
                    db_path=path,
                    base_name=label,
                    progress_callback=progress_callback,
                )
                agent_evidence.extend(dictionary_evidence)
            elif route == "tables":
                answer = _answer_tables(conn, limit=limit)
            elif route == "sample":
                answer = _answer_sample(conn, query, limit=min(limit, 50))
            elif route == "feasibility":
                try:
                    answer, feasibility_evidence = _answer_feasibility(
                        conn,
                        query,
                        limit=max(limit, 80),
                        llm_model=llm_model,
                        progress_callback=progress_callback,
                        db_path=path,
                        base_name=label,
                    )
                    agent_evidence.extend(feasibility_evidence)
                except LIAClientError as exc:
                    answer = _answer_schema(
                        conn,
                        limit=limit,
                        db_path=path,
                        base_name=label,
                        progress_callback=progress_callback,
                    )
                    agent_evidence.append(f"Viabilidade LLM indisponivel; exibindo schema: {exc}")
            elif route == "sql":
                try:
                    answer, sql_evidence, agent_meta = answer_sql_agent_query(
                        conn,
                        query,
                        llm_model=llm_model,
                        limit=max(limit, 100),
                        progress_callback=progress_callback,
                        schema=_get_schema(
                            conn,
                            db_path=path,
                            limit=200,
                            base_name=label,
                            progress_callback=progress_callback,
                        ),
                    )
                    agent_evidence.extend(sql_evidence or [])
                except Exception as exc:
                    answer = (
                        "O router classificou esta pergunta como consulta analitica SQL, "
                        "mas o SQL Agent nao conseguiu gerar/executar a consulta.\n\n"
                        f"Motivo: `{exc}`\n\n"
                        "Use `@sqlite esquema da base` ou `@sqlite quais tabelas existem?` "
                        "para conferir os nomes de tabelas e colunas."
                    )
                    agent_evidence.append(f"SQL Agent indisponivel para rota `sql`: {exc}")
            else:
                answer = _answer_overview(conn, path, limit=limit)

        label_text = f" ({label})" if label else ""
        evidence_items = [
            f"- Banco consultado{label_text}: `{path}`",
            "- Consulta via SQLite universal.",
        ]
        evidence_items.extend(f"- {item}" for item in agent_evidence)

        final_output = (
            f"# Resposta\n\n{answer}\n\n"
            "---\n\n"
            "# Evidencia\n\n"
            + "\n\n".join(evidence_items)
        )
        routing = {"strategy": "sqlite_universal", "db_path": str(path), "found": True}
        if agent_meta:
            routing["sql_agent"] = agent_meta
        return final_output, [], routing
    finally:
        conn.close()
