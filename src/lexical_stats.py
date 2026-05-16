import re


# =====================================================
# EXTRAIR FRASE OU TERMO DA PERGUNTA
# =====================================================

def extract_term(query):

    # detectar frase entre aspas
    phrase = re.findall(r'"([^"]+)"', query)

    if phrase:
        return phrase[0].lower()

    # fallback simples
    words = re.findall(r"\w+", query.lower())

    # remover palavras comuns
    stop = {"quantas", "vezes", "aparece", "termo", "documentos", "em", "que"}

    words = [w for w in words if w not in stop]

    return " ".join(words)


# =====================================================
# CONTAGEM DETERMINÍSTICA
# =====================================================

def phrase_frequency(index, phrase):

    phrase = phrase.lower().strip()
    terms = phrase.split()

    if not terms:
        return {}

    # caso de um único termo
    if len(terms) == 1:

        term = terms[0]

        if term not in index:
            return {}

        return {
            doc: len(pos)
            for doc, pos in index[term].items()
        }

    results = {}

    first = terms[0]

    if first not in index:
        return {}

    for doc, positions in index[first].items():

        # converter para set para busca rápida
        positions_sets = {
            term: set(index[term].get(doc, []))
            for term in terms
            if term in index
        }

        count = 0

        for pos in positions:

            match = True

            for offset, term in enumerate(terms[1:], start=1):

                if term not in positions_sets:
                    match = False
                    break

                if (pos + offset) not in positions_sets[term]:
                    match = False
                    break

            if match:
                count += 1

        if count > 0:
            results[doc] = count

    return results