import re
import pickle
from collections import defaultdict

from src.config import get_chunks_path, get_positional_index_path
from src.safe_jsonl import load_valid_jsonl


# =====================================================
# TOKENIZAÇÃO
# =====================================================

def tokenize(text):
    return re.findall(r"\w+", text.lower())


# =====================================================
# BUILD POSITIONAL INDEX
# =====================================================

def build_positional_index(base):

    chunks = load_valid_jsonl(get_chunks_path(base))

    index = defaultdict(lambda: defaultdict(list))

    for chunk in chunks:

        tokens = tokenize(chunk["text"])
        chunk_id = chunk["chunk_id"]

        for pos, token in enumerate(tokens):
            index[token][chunk_id].append(pos)

    path = get_positional_index_path(base)

    with open(path, "wb") as f:
        pickle.dump(dict(index), f)

    print(f"[OK] Positional index criado ({len(index)} termos)")


# =====================================================
# LOAD INDEX
# =====================================================

def load_positional_index(base):

    path = get_positional_index_path(base)

    with open(path, "rb") as f:
        return pickle.load(f)


# =====================================================
# PHRASE SEARCH
# =====================================================

def phrase_search(phrase, index):

    tokens = tokenize(phrase)

    if not tokens:
        return set()

    postings = [index.get(t, {}) for t in tokens]

    common_chunks = set(postings[0].keys())

    for p in postings[1:]:
        common_chunks &= set(p.keys())

    results = set()

    for chunk in common_chunks:

        base_positions = postings[0][chunk]

        for pos in base_positions:

            match = True

            for i in range(1, len(tokens)):

                if pos + i not in postings[i][chunk]:
                    match = False
                    break

            if match:
                results.add(chunk)
                break

    return results

# =====================================================
# PROXIMITY SEARCH (NEAR)
# =====================================================

def near_search(term1, term2, max_distance, index):

    term1_tokens = tokenize(term1)
    term2_tokens = tokenize(term2)

    if len(term1_tokens) != 1 or len(term2_tokens) != 1:
        return set()

    term1 = term1_tokens[0]
    term2 = term2_tokens[0]

    postings1 = index.get(term1, {})
    postings2 = index.get(term2, {})

    common_chunks = set(postings1.keys()) & set(postings2.keys())

    results = set()

    for chunk in common_chunks:

        positions1 = postings1[chunk]
        positions2 = postings2[chunk]

        for p1 in positions1:
            for p2 in positions2:

                if abs(p1 - p2) <= max_distance:
                    results.add(chunk)
                    break

            if chunk in results:
                break

    return results

# =====================================================
# BOOLEAN SEARCH
# =====================================================

def _query_tokens(query):

    return re.findall(r'"[^"]+"|\S+', query)


def _condition_docs(condition, index):

    condition = condition.strip()

    if not condition:
        return set()

    near_pattern = re.fullmatch(
        r"(.+?)\s+near/(\d+)\s+(.+)",
        condition,
        flags=re.IGNORECASE,
    )

    if near_pattern:
        return near_search(
            near_pattern.group(1),
            near_pattern.group(3),
            int(near_pattern.group(2)),
            index,
        )

    if condition.startswith('"') and condition.endswith('"'):
        condition = condition[1:-1]

    tokens = tokenize(condition)

    if not tokens:
        return set()

    if len(tokens) > 1:
        return phrase_search(" ".join(tokens), index)

    return set(index.get(tokens[0], {}).keys())


def _all_chunks(index):

    chunks = set()

    for postings in index.values():
        chunks.update(postings.keys())

    return chunks


def search(query, index):

    raw_tokens = _query_tokens(query)
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

    result = None
    operator = "OR"

    for token in tokens:

        token_upper = token.upper()

        if token_upper == "AND":
            operator = "AND"
            continue

        if token_upper == "OR":
            operator = "OR"
            continue

        if token_upper == "NOT":
            operator = "NOT"
            continue

        docs = _condition_docs(token, index)

        if result is None:
            result = _all_chunks(index) - docs if operator == "NOT" else docs
            continue

        if operator == "AND":
            result &= docs

        elif operator == "OR":
            result |= docs

        elif operator == "NOT":
            result -= docs

    return result if result else set()
