import re

from src.positional_index import search


def parse_boolean_query(query):
    query = (query or "").strip()

    if not query:
        return [], "OR", []

    raw_tokens = re.findall(r'"[^"]+"|\S+', query)
    tokens = []
    i = 0

    while i < len(raw_tokens):
        if (
            i + 2 < len(raw_tokens)
            and re.fullmatch(r"near/\d+", raw_tokens[i + 1], flags=re.IGNORECASE)
        ):
            tokens.append(f"{raw_tokens[i]} {raw_tokens[i + 1]} {raw_tokens[i + 2]}")
            i += 3
            continue

        tokens.append(raw_tokens[i])
        i += 1

    terms = []
    exclude = []
    operator = "OR"
    i = 0

    while i < len(tokens):
        token = tokens[i].upper()

        if token == "AND":
            operator = "AND"
        elif token == "OR":
            operator = "OR"
        elif token == "NOT":
            if i + 1 < len(tokens):
                exclude.append(tokens[i + 1].lower())
                i += 1
        else:
            terms.append(tokens[i].lower())

        i += 1

    if '"' in query:
        operator = "AND"

    return terms, operator, exclude


def strip_lexical_syntax(term):
    term = (term or "").strip()

    if term.startswith('"') and term.endswith('"'):
        return term[1:-1].strip()

    return term


def _is_quoted(term):
    term = (term or "").strip()
    return term.startswith('"') and term.endswith('"')


def _looks_like_doc_reference(term):
    term = strip_lexical_syntax(term).lower()
    return (
        ".pdf" in term
        or "_" in term
        or "\\" in term
        or "/" in term
    )


def _should_union_doc_name_terms(terms, operator):
    return (
        operator == "AND"
        and len(terms) > 1
        and all(_is_quoted(term) for term in terms)
        and all(_looks_like_doc_reference(term) for term in terms)
    )


def all_docs_from_positional_index(index):
    docs = set()

    for postings in index.values():
        docs.update(c.split("::")[0] for c in postings.keys())

    return docs


def docs_from_lexical_query_parts(index, terms, operator, exclude):
    if not index:
        return set()

    docs_conteudo = set()

    if not terms and exclude:
        docs_conteudo = all_docs_from_positional_index(index)

    for term in terms:
        results = search(term, index)
        docs = set(c.split("::")[0] for c in results)

        if not docs_conteudo:
            docs_conteudo = docs
        else:
            if operator == "AND":
                docs_conteudo &= docs
            else:
                docs_conteudo |= docs

    for term in exclude:
        results = search(term, index)
        docs = set(c.split("::")[0] for c in results)
        docs_conteudo -= docs

    return docs_conteudo


def doc_name_matches_lexical_query(doc_name, raw_query, terms, operator, exclude):
    name = (doc_name or "").lower()

    if not (raw_query or "").strip():
        return False

    if not terms and exclude:
        include_match = True
    else:
        matches = [
            strip_lexical_syntax(term).lower() in name
            for term in terms
            if strip_lexical_syntax(term)
        ]

        if _should_union_doc_name_terms(terms, operator):
            include_match = any(matches)
        else:
            include_match = all(matches) if operator == "AND" else any(matches)

    if not include_match:
        return False

    for term in exclude:
        term_clean = strip_lexical_syntax(term).lower()
        if term_clean and term_clean in name:
            return False

    return True
