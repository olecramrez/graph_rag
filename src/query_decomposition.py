import re

def decompose_query(query):

    q = query.lower()

    # separa perguntas compostas
    if " e " in q:
        return [q1.strip() for q1 in q.split(" e ")]

    if "?" in query and query.count("?") > 1:
        return [q.strip() for q in query.split("?") if q.strip()]

    # padrões comuns
    patterns = [
        r"(.*) e em quais documentos",
        r"(.*) e quantas vezes",
        r"(.*) e onde aparece"
    ]

    for p in patterns:
        m = re.match(p, q)
        if m:
            base = m.group(1)
            return [
                base,
                f"em quais documentos {base}",
            ]

    return [query]